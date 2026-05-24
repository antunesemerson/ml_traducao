from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import db


SCRIPT_NAME = "micro_review_queue"
KEY_RE = re.compile(r"^\s*(?P<key>[^#\s][^:]*):")
GENDER_ROOT_RE = re.compile(
    r"(?P<word>[A-Za-zÀ-ÖØ-öø-ÿ]{3,}[ao])"
    r"(?P<token>\[[^\]\n]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\])",
    re.IGNORECASE,
)
RAW_LEVIES_RE = re.compile(r"(?<![\[\w])levies(?![\]\w])", re.IGNORECASE)
LEADING_ARTICLE_TITLE_RE = re.compile(
    r"(?<![A-Za-zÀ-ÖØ-öø-ÿ])(?P<article>[oa])(?P<name>[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'_-]{3,})"
)
LOWER_RELATION_OPENING_RE = re.compile(
    r"(^|\\n)(?P<relation>primo|prima|sobrinho|sobrinha|filho|filha|pai|mãe|irmão|irmã|"
    r"meio-irmão|meia-irmã|neto|neta|avô|avó|tio|tia|cunhado|cunhada),"
)


@dataclass(frozen=True)
class QueueItem:
    category: str
    package_name: str
    relative_path: str
    line_number: int
    segment_id: int | None
    source_key: str | None
    current_fragment: str
    suggested_fragment: str
    text_preview: str


def short(value: str, limit: int = 300) -> str:
    text = value.replace("\t", "\\t").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def package_roots(settings: dict) -> dict[str, Path]:
    return {
        "spanish_old": db.project_path(settings["spanish_traduzido_old"]),
        "output_spanish": db.project_path(settings["output_spanish"]),
    }


def load_segment_index(settings: dict) -> dict[tuple[str, str], int]:
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = conn.execute(
            """
            SELECT id, relative_path, source_key
            FROM source_segments
            WHERE is_active = 1
            """
        ).fetchall()
    return {(row["relative_path"], row["source_key"]): int(row["id"]) for row in rows}


def is_inside_token(line: str, position: int) -> bool:
    last_open = line.rfind("[", 0, position)
    last_close = line.rfind("]", 0, position)
    return last_open > last_close


def is_inside_dollar_variable(line: str, start: int, end: int) -> bool:
    return (start > 0 and line[start - 1] == "$") or (end < len(line) and line[end] == "$")


def scan_line(
    package_name: str,
    relative_path: str,
    line_number: int,
    line: str,
    scan_text: str,
    segment_id: int | None,
    source_key: str | None,
) -> list[QueueItem]:
    items: list[QueueItem] = []
    preview = short(line)

    for match in GENDER_ROOT_RE.finditer(scan_text):
        word = match.group("word")
        token = match.group("token")
        items.append(
            QueueItem(
                category="gender_token_complete_root",
                package_name=package_name,
                relative_path=relative_path,
                line_number=line_number,
                segment_id=segment_id,
                source_key=source_key,
                current_fragment=f"{word}{token}",
                suggested_fragment=f"{word[:-1]}{token}",
                text_preview=preview,
            )
        )

    for match in RAW_LEVIES_RE.finditer(scan_text):
        if is_inside_token(scan_text, match.start()):
            continue
        if is_inside_dollar_variable(scan_text, match.start(), match.end()):
            continue
        items.append(
            QueueItem(
                category="raw_levies_literal",
                package_name=package_name,
                relative_path=relative_path,
                line_number=line_number,
                segment_id=segment_id,
                source_key=source_key,
                current_fragment=match.group(0),
                suggested_fragment="levas",
                text_preview=preview,
            )
        )

    for match in LEADING_ARTICLE_TITLE_RE.finditer(scan_text):
        if is_inside_token(scan_text, match.start()):
            continue
        name = match.group("name")
        if name in {"O", "A"}:
            continue
        items.append(
            QueueItem(
                category="leading_article_attached_title",
                package_name=package_name,
                relative_path=relative_path,
                line_number=line_number,
                segment_id=segment_id,
                source_key=source_key,
                current_fragment=match.group(0),
                suggested_fragment=name,
                text_preview=preview,
            )
        )

    for match in LOWER_RELATION_OPENING_RE.finditer(scan_text):
        relation = match.group("relation")
        items.append(
            QueueItem(
                category="lowercase_relation_opening",
                package_name=package_name,
                relative_path=relative_path,
                line_number=line_number,
                segment_id=segment_id,
                source_key=source_key,
                current_fragment=f"{relation},",
                suggested_fragment=f"{relation[:1].upper()}{relation[1:]},",
                text_preview=preview,
            )
        )

    return items


def scan_file(package_name: str, root: Path, path: Path, segment_index: dict[tuple[str, str], int]) -> list[QueueItem]:
    relative_path = path.relative_to(root).as_posix()
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    items: list[QueueItem] = []
    for line_number, line in enumerate(lines, start=1):
        if line.lstrip().startswith("#"):
            continue
        key_match = KEY_RE.match(line)
        source_key = key_match.group("key").strip() if key_match else None
        segment_id = segment_index.get((relative_path, source_key)) if source_key else None
        scan_text = line.split(":", 1)[1] if source_key and ":" in line else line
        items.extend(scan_line(package_name, relative_path, line_number, line, scan_text, segment_id, source_key))
    return items


def build_queue(settings: dict, package_filter: set[str] | None) -> list[QueueItem]:
    segment_index = load_segment_index(settings)
    items: list[QueueItem] = []
    for package_name, root in package_roots(settings).items():
        if package_filter and package_name not in package_filter:
            continue
        print(f"[{SCRIPT_NAME}] Scanning {package_name}: {root}")
        if not root.exists():
            continue
        files = sorted(root.rglob("*.yml"))
        for idx, path in enumerate(files, start=1):
            if idx % 500 == 0:
                print(f"[{SCRIPT_NAME}]   {package_name}: {idx}/{len(files)} files")
            items.extend(scan_file(package_name, root, path, segment_index))
    return items


def write_csv(settings: dict, items: list[QueueItem]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"{timestamp}_{SCRIPT_NAME}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "category",
                "package_name",
                "relative_path",
                "line_number",
                "segment_id",
                "source_key",
                "current_fragment",
                "suggested_fragment",
                "text_preview",
            ],
        )
        writer.writeheader()
        for item in items:
            writer.writerow(item.__dict__)
    return path


def write_report(settings: dict, items: list[QueueItem], csv_path: Path, sample_limit: int) -> Path:
    by_category = Counter(item.category for item in items)
    lines = [
        "Micro review queue",
        f"CSV: {csv_path}",
        f"Items: {len(items)}",
        "",
        "By category:",
        *[f"- {category}: {count}" for category, count in by_category.most_common()],
        "",
        f"First {min(sample_limit, len(items))} samples:",
    ]
    for item in items[:sample_limit]:
        lines.append(
            f"- {item.category} {item.package_name}:{item.relative_path}:{item.line_number} "
            f"segment={item.segment_id or '?'} key={item.source_key or '?'} "
            f"{item.current_fragment} -> {item.suggested_fragment}"
        )
        lines.append(f"  {item.text_preview}")
    return db.write_report(settings, SCRIPT_NAME, lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a categorized review queue for small visual/text issues.")
    parser.add_argument(
        "--package",
        action="append",
        choices=["spanish_old", "output_spanish"],
        help="Package to scan. Can be repeated. Default scans spanish_old and output_spanish.",
    )
    parser.add_argument("--sample-limit", type=int, default=120)
    args = parser.parse_args()

    settings = db.load_settings()
    items = build_queue(settings, set(args.package) if args.package else None)
    csv_path = write_csv(settings, items)
    report_path = write_report(settings, items, csv_path, args.sample_limit)

    print(f"[{SCRIPT_NAME}] Items: {len(items)}")
    print(f"[{SCRIPT_NAME}] CSV: {csv_path}")
    print(f"[{SCRIPT_NAME}] Report: {report_path}")
    print(f"[{SCRIPT_NAME}] Done")


if __name__ == "__main__":
    main()
