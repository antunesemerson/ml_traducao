from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime

import db


RULE_VERSION = "finalization_queue_v1"

SPANISH_RESIDUE_RE = re.compile(
    r"[¿¡«»]|"
    r"\b("
    r"cortesanos|situaciones|decisiones|rechaza|rechazo|rechazó|"
    r"vasall|levies|tu personaje|una buena|un buen|la poetisa|el poeta|"
    r"del | de la | de los | de las | los | las | el | la | un | una |"
    r"sí|ha |es |está|están|será|puede|pueden|debe|deben"
    r")\b",
    re.IGNORECASE,
)
TOKEN_SPACING_RE = re.compile(r"\][A-Za-zÀ-ÿ]|[A-Za-zÀ-ÿ]\[")
GENDER_SUFFIX_RE = re.compile(
    r"Custom\('ES_[A-Za-z]+'\)\][ao]s?\b|"
    r"Custom\('ES_[A-Za-z]+'\)\][aáéíóúâêôãõç]",
    re.IGNORECASE,
)
MOJIBAKE_RE = re.compile(r"Ã|Â|�")


def fetch_pending(conn) -> list[dict]:
    rows = conn.execute(
        """
        WITH pkg AS (
            SELECT
                s.relative_path,
                COUNT(*) AS package_total,
                SUM(CASE WHEN sc.segment_id IS NULL THEN 1 ELSE 0 END) AS package_pending
            FROM source_segments s
            LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
            WHERE s.is_active = 1
            GROUP BY s.relative_path
        ),
        high_impact AS (
            SELECT relative_path, 1 AS is_high_impact
            FROM package_focus_queue
            WHERE focus_group = 'high_impact_v1'
        )
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            pkg.package_total,
            pkg.package_pending,
            COALESCE(high_impact.is_high_impact, 0) AS is_high_impact
        FROM source_segments s
        JOIN pkg ON pkg.relative_path = s.relative_path
        LEFT JOIN high_impact ON high_impact.relative_path = s.relative_path
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND sc.segment_id IS NULL
        ORDER BY s.relative_path, s.source_line_number, s.id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def macro_path_weight(path: str) -> float:
    score = 0.0
    if path.startswith("gui/") or any(token in path for token in ("window", "hud", "menu", "interface")):
        score += 220
    if any(token in path for token in ("interactions", "decisions", "effects", "triggers", "buildings", "artifacts")):
        score += 150
    if any(token in path for token in ("activity", "activities", "travel", "court", "council")):
        score += 120
    if path.startswith("names/") or "nicknames" in path:
        score += 80
    return score


def classify(row: dict) -> tuple[str, str, str, list[str], float]:
    path = row["relative_path"]
    old_text = row["old_text"] or ""
    spanish_text = row["spanish_text"] or ""
    text_length = len(old_text)
    reasons: list[str] = []

    has_mojibake = bool(MOJIBAKE_RE.search(old_text))
    has_token_spacing = bool(TOKEN_SPACING_RE.search(old_text))
    has_gender_suffix = bool(GENDER_SUFFIX_RE.search(old_text))
    has_spanish_residue = bool(SPANISH_RESIDUE_RE.search(old_text))
    spanish_source_has_residue = bool(SPANISH_RESIDUE_RE.search(spanish_text))

    if has_mojibake:
        reasons.append("mojibake")
    if has_token_spacing:
        reasons.append("token_spacing")
    if has_gender_suffix:
        reasons.append("gender_suffix_after_token")
    if has_spanish_residue:
        reasons.append("spanish_residue_in_old")
    if spanish_source_has_residue:
        reasons.append("spanish_source_reference")
    if row["is_high_impact"]:
        reasons.append("high_impact_package")

    if path == "names/character_names_l_spanish.yml":
        bucket = "names_cautious"
        risk = "medium"
        action = "preserve_or_apply_reviewed_name_rule"
    elif path == "nicknames_l_spanish.yml":
        bucket = "nicknames_batch"
        risk = "medium"
        action = "review_nickname_batch"
    elif text_length > 800:
        bucket = "long_review"
        risk = "high"
        action = "codex_translate_review"
    elif has_mojibake or has_token_spacing or has_gender_suffix:
        bucket = "structure_fix"
        risk = "high" if text_length > 300 else "medium"
        action = "mechanical_fix_then_review"
    elif has_spanish_residue:
        bucket = "residual_spanish"
        risk = "medium" if text_length <= 300 else "high"
        action = "translate_residue_review"
    elif text_length <= 80:
        bucket = "short_safe_review"
        risk = "low"
        action = "batch_confirm_or_minor_fix"
    elif text_length <= 300:
        bucket = "medium_batch"
        risk = "medium"
        action = "batch_review"
    else:
        bucket = "narrative_batch"
        risk = "medium"
        action = "codex_batch_review"

    score = float(row["package_pending"])
    score += macro_path_weight(path)
    if row["is_high_impact"]:
        score += 300
    if bucket == "structure_fix":
        score += 260
    elif bucket == "residual_spanish":
        score += 220
    elif bucket == "nicknames_batch":
        score += 210
    elif bucket == "names_cautious":
        score += 180
    elif bucket == "short_safe_review":
        score += 130
    elif bucket == "long_review":
        score -= 80
    score -= min(text_length / 50, 40)

    return bucket, risk, action, reasons, score


def rebuild_queue(conn, rows: list[dict]) -> Counter:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("DELETE FROM finalization_queue")
    counts: Counter[str] = Counter()
    for row in rows:
        bucket, risk, action, reasons, score = classify(row)
        counts[bucket] += 1
        conn.execute(
            """
            INSERT INTO finalization_queue (
                segment_id, relative_path, source_key, source_line_number,
                closure_bucket, risk_level, action_hint, priority_score,
                text_length, package_pending, package_total, is_high_impact,
                status, reasons_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
            """,
            (
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                bucket,
                risk,
                action,
                score,
                len(row["old_text"] or ""),
                row["package_pending"],
                row["package_total"],
                row["is_high_impact"],
                json.dumps(reasons, ensure_ascii=False),
                now,
                now,
            ),
        )
    conn.commit()
    return counts


def top_packages(conn, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            relative_path,
            COUNT(*) AS pending,
            SUM(CASE WHEN risk_level = 'high' THEN 1 ELSE 0 END) AS high_risk,
            SUM(CASE WHEN is_high_impact = 1 THEN 1 ELSE 0 END) AS high_impact_rows,
            ROUND(AVG(priority_score), 1) AS avg_score,
            GROUP_CONCAT(DISTINCT closure_bucket) AS buckets
        FROM finalization_queue
        WHERE status = 'open'
        GROUP BY relative_path
        ORDER BY MAX(priority_score) DESC, pending DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def top_segments(conn, limit: int, bucket: str | None) -> list[dict]:
    params: list[object] = []
    where = "status = 'open'"
    if bucket:
        where += " AND closure_bucket = ?"
        params.append(bucket)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            segment_id, relative_path, source_key, closure_bucket, risk_level,
            action_hint, priority_score, text_length, reasons_json
        FROM finalization_queue
        WHERE {where}
        ORDER BY priority_score DESC, text_length ASC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def main(limit: int | None = None, bucket: str | None = None, top_limit: int = 40) -> None:
    settings = db.load_settings()
    started_at = datetime.now()

    print("[build_finalization_queue] Starting finalization queue build")
    print(f"[build_finalization_queue] Rule version: {RULE_VERSION}")
    print(f"[build_finalization_queue] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        pending = fetch_pending(conn)
        if limit is not None:
            pending = pending[:limit]
        counts = rebuild_queue(conn, pending)
        risk_counts = Counter(
            row["risk_level"]
            for row in conn.execute("SELECT risk_level FROM finalization_queue WHERE status = 'open'")
        )
        top_pkg = top_packages(conn, top_limit)
        top_seg = top_segments(conn, top_limit, bucket)

    elapsed = datetime.now() - started_at
    report_lines = [
        "Finalization queue report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Pending queued: {sum(counts.values())}",
        "",
        "Bucket counts:",
    ]
    for name, count in counts.most_common():
        report_lines.append(f"- {name}: {count}")
    report_lines.extend(["", "Risk counts:"])
    for name, count in risk_counts.most_common():
        report_lines.append(f"- {name}: {count}")
    report_lines.extend(["", "Top packages:"])
    for item in top_pkg:
        report_lines.append(
            (
                f"- score={item['avg_score']} | pending={item['pending']} | "
                f"high_risk={item['high_risk']} | high_impact_rows={item['high_impact_rows']} | "
                f"{item['relative_path']} | buckets={item['buckets']}"
            )
        )
    report_lines.extend(["", f"Top segments{f' bucket={bucket}' if bucket else ''}:"])
    for item in top_seg:
        report_lines.append(
            (
                f"- score={item['priority_score']:.1f} | len={item['text_length']} | "
                f"{item['closure_bucket']}/{item['risk_level']} | "
                f"{item['segment_id']} | {item['relative_path']} | {item['source_key']} | "
                f"reasons={item['reasons_json']}"
            )
        )

    report_path = db.write_report(settings, "build_finalization_queue", report_lines)
    print(f"[build_finalization_queue] Queued: {sum(counts.values())}")
    print("[build_finalization_queue] Bucket counts:")
    for name, count in counts.most_common():
        print(f"[build_finalization_queue]   {name}: {count}")
    print("[build_finalization_queue] Risk counts:")
    for name, count in risk_counts.most_common():
        print(f"[build_finalization_queue]   {name}: {count}")
    print("[build_finalization_queue] Top packages:")
    for item in top_pkg[:10]:
        print(
            f"[build_finalization_queue]   pending={item['pending']:3d} | "
            f"avg_score={item['avg_score']} | {item['relative_path']}"
        )
    print(f"[build_finalization_queue] Report: {report_path}")
    print("[build_finalization_queue] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build an optimized finalization queue for unresolved segments.")
    parser.add_argument("--limit", type=int, default=None, help="Optional max pending rows to queue.")
    parser.add_argument("--bucket", default=None, help="Optional bucket filter for the top-segments report.")
    parser.add_argument("--top-limit", type=int, default=40, help="Number of top packages/segments in the report.")
    args = parser.parse_args()
    main(limit=args.limit, bucket=args.bucket, top_limit=args.top_limit)
