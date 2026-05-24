from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import db


SCRIPT_NAME = "gender_token_audit"
GENDER_ROOT_RE = re.compile(
    r"(?P<word>[A-Za-zÀ-ÖØ-öø-ÿ]{3,}[ao])"
    r"(?P<token>\[[^\]\n]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\])",
    re.IGNORECASE,
)
KEY_RE = re.compile(r"^\s*(?P<key>[^#\s][^:]*):")


@dataclass(frozen=True)
class Hit:
    package_name: str
    relative_path: str
    line_number: int
    segment_id: int | None
    source_key: str | None
    word: str
    token: str
    suggested_fragment: str
    text_preview: str


def short(value: str, limit: int = 260) -> str:
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


def scan_file(package_name: str, root: Path, path: Path, segment_index: dict[tuple[str, str], int]) -> list[Hit]:
    hits: list[Hit] = []
    relative_path = path.relative_to(root).as_posix()
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    for line_number, line in enumerate(lines, start=1):
        if "ES_OA" not in line and "ES_AO" not in line:
            continue
        key_match = KEY_RE.match(line)
        source_key = key_match.group("key").strip() if key_match else None
        segment_id = segment_index.get((relative_path, source_key)) if source_key else None
        for match in GENDER_ROOT_RE.finditer(line):
            word = match.group("word")
            token = match.group("token")
            suggested_fragment = f"{word[:-1]}{token}"
            hits.append(
                Hit(
                    package_name=package_name,
                    relative_path=relative_path,
                    line_number=line_number,
                    segment_id=segment_id,
                    source_key=source_key,
                    word=word,
                    token=token,
                    suggested_fragment=suggested_fragment,
                    text_preview=short(line),
                )
            )
    return hits


def scan_packages(settings: dict, package_filter: set[str] | None = None) -> list[Hit]:
    segment_index = load_segment_index(settings)
    hits: list[Hit] = []
    for package_name, root in package_roots(settings).items():
        if package_filter and package_name not in package_filter:
            continue
        print(f"[{SCRIPT_NAME}] Scanning {package_name}: {root}")
        if not root.exists():
            print(f"[{SCRIPT_NAME}] Missing package root: {root}")
            continue
        files = sorted(root.rglob("*.yml"))
        for idx, path in enumerate(files, start=1):
            if idx % 500 == 0:
                print(f"[{SCRIPT_NAME}]   {package_name}: {idx}/{len(files)} files")
            hits.extend(scan_file(package_name, root, path, segment_index))
    return hits


def write_csv(settings: dict, hits: list[Hit]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"{timestamp}_{SCRIPT_NAME}.csv"
    with report_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "package_name",
                "relative_path",
                "line_number",
                "segment_id",
                "source_key",
                "word",
                "token",
                "suggested_fragment",
                "text_preview",
            ],
        )
        writer.writeheader()
        for hit in hits:
            writer.writerow(
                {
                    "package_name": hit.package_name,
                    "relative_path": hit.relative_path,
                    "line_number": hit.line_number,
                    "segment_id": hit.segment_id or "",
                    "source_key": hit.source_key or "",
                    "word": hit.word,
                    "token": hit.token,
                    "suggested_fragment": hit.suggested_fragment,
                    "text_preview": hit.text_preview,
                }
            )
    return report_path


def write_summary(settings: dict, hits: list[Hit], csv_path: Path, sample_limit: int) -> Path:
    by_package = Counter(hit.package_name for hit in hits)
    by_word = Counter(hit.word.lower() for hit in hits)
    lines = [
        "Gender token audit",
        f"CSV: {csv_path}",
        f"Hits: {len(hits)}",
        "",
        "By package:",
        *[f"- {name}: {count}" for name, count in by_package.most_common()],
        "",
        "Top repeated roots:",
        *[f"- {word}: {count}" for word, count in by_word.most_common(30)],
        "",
        f"First {min(sample_limit, len(hits))} samples:",
    ]
    for hit in hits[:sample_limit]:
        lines.append(
            f"- {hit.package_name}:{hit.relative_path}:{hit.line_number} "
            f"segment={hit.segment_id or '?'} key={hit.source_key or '?'} "
            f"{hit.word}{hit.token} -> {hit.suggested_fragment}"
        )
        lines.append(f"  {hit.text_preview}")
    return db.write_report(settings, SCRIPT_NAME, lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit possible complete masculine/feminine roots before ES_OA/ES_AO gender tokens."
    )
    parser.add_argument(
        "--package",
        action="append",
        choices=["spanish_old", "output_spanish"],
        help="Package to scan. Can be repeated. Default scans spanish_old and output_spanish.",
    )
    parser.add_argument("--sample-limit", type=int, default=80, help="Samples listed in the text report.")
    args = parser.parse_args()

    settings = db.load_settings()
    package_filter = set(args.package) if args.package else None

    print(f"[{SCRIPT_NAME}] Starting gender token audit")
    hits = scan_packages(settings, package_filter)
    csv_path = write_csv(settings, hits)
    report_path = write_summary(settings, hits, csv_path, args.sample_limit)

    print(f"[{SCRIPT_NAME}] Hits: {len(hits)}")
    print(f"[{SCRIPT_NAME}] CSV: {csv_path}")
    print(f"[{SCRIPT_NAME}] Report: {report_path}")
    print(f"[{SCRIPT_NAME}] Done")


if __name__ == "__main__":
    main()
