from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime

import db


RULE_VERSION = "package_priority_report_v1"

SPANISH_TERMS = [
    "cortesanos",
    "situaciones",
    "decisiones",
    "rechaza",
    "vasallos",
    "señor",
    "señora",
    "¿",
    "¡",
    "«",
    "»",
    "Compañ",
    "Caballeros",
    "Orden",
    "Imperio",
    "Reino",
    "Llamadores",
    " de ",
    " del ",
    " la ",
    " las ",
    " los ",
    " el ",
]


def priority_score(path: str, pending: int, total: int, residue_hits: int) -> float:
    score = float(pending + residue_hits * 2)
    if path.startswith("gui/"):
        score += 600
    if any(token in path for token in ("window", "interface", "hud", "menu", "court", "council", "activity", "travel")):
        score += 250
    if any(token in path for token in ("core", "concept", "common")):
        score += 300
    if total < 80:
        score += 120
    return score


def fetch_pending_packages(conn, limit: int | None) -> list[dict]:
    limit_sql = ""
    params: list[object] = []
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            s.relative_path,
            COUNT(*) AS total_segments,
            SUM(CASE WHEN sc.segment_id IS NOT NULL THEN 1 ELSE 0 END) AS confirmed_segments,
            SUM(CASE WHEN sc.segment_id IS NULL THEN 1 ELSE 0 END) AS pending_segments,
            SUM(CASE WHEN sc.confirmation_level = 'human_confirmed' THEN 1 ELSE 0 END) AS human_confirmed,
            SUM(CASE WHEN sc.confirmation_level = 'auto_confirmed' THEN 1 ELSE 0 END) AS auto_confirmed
        FROM source_segments s
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
        GROUP BY s.relative_path
        HAVING pending_segments > 0
        ORDER BY pending_segments DESC, total_segments DESC
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def residue_summary(conn, relative_path: str, sample_limit: int) -> tuple[int, Counter[str]]:
    rows = conn.execute(
        """
        SELECT s.old_text
        FROM source_segments s
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND sc.segment_id IS NULL
          AND s.relative_path = ?
        ORDER BY s.id
        LIMIT ?
        """,
        (relative_path, sample_limit),
    ).fetchall()
    hits: Counter[str] = Counter()
    total = 0
    for row in rows:
        text = row["old_text"] or ""
        for term in SPANISH_TERMS:
            if term in text:
                hits[term] += 1
                total += 1
    return total, hits


def format_hits(hits: Counter[str], limit: int = 6) -> str:
    if not hits:
        return "-"
    return ", ".join(f"{term.strip() or repr(term)}:{count}" for term, count in hits.most_common(limit))


def main(limit: int | None = None, sample_limit: int = 120) -> None:
    settings = db.load_settings()
    started_at = datetime.now()

    print("[package_priority_report] Starting package priority report")
    print(f"[package_priority_report] Rule version: {RULE_VERSION}")
    print(f"[package_priority_report] Package limit: {limit or 'none'}")
    print(f"[package_priority_report] Sample limit: {sample_limit}")
    print(f"[package_priority_report] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        packages = fetch_pending_packages(conn, limit)
        items: list[dict] = []
        for package in packages:
            residue_hits, hits = residue_summary(conn, package["relative_path"], sample_limit)
            package["residue_hits"] = residue_hits
            package["term_hits"] = hits
            package["score"] = priority_score(
                package["relative_path"],
                package["pending_segments"],
                package["total_segments"],
                residue_hits,
            )
            items.append(package)

    items.sort(key=lambda item: (item["score"], item["pending_segments"], item["relative_path"]), reverse=True)
    small_items = [item for item in items if item["pending_segments"] <= 120]

    elapsed = datetime.now() - started_at
    report_lines = [
        "Package priority report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Package limit: {limit or 'none'}",
        f"Residue sample limit: {sample_limit}",
        "",
        "Top packages by impact score:",
    ]
    for item in items[:80]:
        confirmed_pct = (item["confirmed_segments"] or 0) / item["total_segments"] * 100
        report_lines.append(
            (
                f"- score={item['score']:.1f} | pending={item['pending_segments']} | "
                f"total={item['total_segments']} | confirmed={confirmed_pct:.2f}% | "
                f"residue_sample={item['residue_hits']} | {item['relative_path']} | "
                f"hits={format_hits(item['term_hits'])}"
            )
        )

    report_lines.extend(["", "Small high-value packages, <=120 pending:"])
    for item in small_items[:100]:
        confirmed_pct = (item["confirmed_segments"] or 0) / item["total_segments"] * 100
        report_lines.append(
            (
                f"- score={item['score']:.1f} | pending={item['pending_segments']} | "
                f"total={item['total_segments']} | confirmed={confirmed_pct:.2f}% | "
                f"{item['relative_path']}"
            )
        )

    report_path = db.write_report(settings, "package_priority_report", report_lines)
    print(f"[package_priority_report] Pending packages inspected: {len(items)}")
    print("[package_priority_report] Top 10:")
    for item in items[:10]:
        print(
            f"[package_priority_report] {item['score']:.1f} | "
            f"pending={item['pending_segments']} | {item['relative_path']}"
        )
    print(f"[package_priority_report] Report: {report_path}")
    print("[package_priority_report] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rank pending localization packages by review impact.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum packages to inspect.")
    parser.add_argument("--sample-limit", type=int, default=120, help="Pending rows sampled per package for residue terms.")
    args = parser.parse_args()
    main(limit=args.limit, sample_limit=args.sample_limit)
