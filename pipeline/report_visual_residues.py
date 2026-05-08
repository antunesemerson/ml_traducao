from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime

import db
import local_quality_validator


RULE_VERSION = "visual_residue_report_v1"
ACTIONABLE_ISSUES = {
    "spanish_punctuation",
    "spanish_residue_in_literal",
    "missing_space_after_token",
    "missing_space_before_token",
    "gender_token_extra_suffix",
    "gender_token_joined_to_word",
    "mojibake_or_unexpected_script",
}


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def fetch_rows(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            sc.confirmed_text,
            sc.confirmation_level,
            sc.locked
        FROM source_segments s
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND COALESCE(sc.locked, 0) = 0
          AND COALESCE(sc.confirmed_text, s.old_text, s.spanish_text, '') != ''
        ORDER BY s.relative_path ASC, s.id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def candidate_text(row: dict) -> str:
    return str(row["confirmed_text"] or row["old_text"] or row["spanish_text"] or "")


def main(limit: int = 20) -> None:
    settings = db.load_settings()
    started_at = datetime.now()

    print("[report_visual_residues] Starting visual residue report")
    print(f"[report_visual_residues] Rule version: {RULE_VERSION}")
    print(f"[report_visual_residues] Validator version: {local_quality_validator.RULE_VERSION}")
    print(f"[report_visual_residues] Database: {db.get_database_path(settings)}")

    issue_counts: Counter[str] = Counter()
    severity_counts: Counter[str] = Counter()
    file_counts: Counter[str] = Counter()
    residue_counts: Counter[str] = Counter()
    samples: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    inspected = 0
    affected_segments: set[int] = set()
    actionable_segments: set[int] = set()

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = fetch_rows(conn)
        for row in rows:
            inspected += 1
            text = candidate_text(row)
            validation = local_quality_validator.validate_text(text)
            issues = validation.get("issues", [])
            if not issues:
                continue
            affected_segments.add(int(row["segment_id"]))
            for issue in issues:
                code = str(issue.get("code") or "unknown")
                severity = str(issue.get("severity") or "unknown")
                issue_counts[code] += 1
                severity_counts[severity] += 1
                if code in ACTIONABLE_ISSUES:
                    actionable_segments.add(int(row["segment_id"]))
                    file_counts[str(row["relative_path"])] += 1
                for match in issue.get("matches") or []:
                    residue_counts[str(match)] += 1
                if len(samples[code]) < limit:
                    samples[code].append((row, issue))

    elapsed = datetime.now() - started_at
    report_lines = [
        "Visual residue report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Validator version: {local_quality_validator.RULE_VERSION}",
        "",
        "Summary:",
        f"- Revisable segments inspected: {inspected}",
        f"- Segments with issues: {len(affected_segments)}",
        f"- Segments with actionable issues: {len(actionable_segments)}",
        "",
        "Actionable issue counts:",
        *[
            f"- {code}: {issue_counts[code]}"
            for code in sorted(ACTIONABLE_ISSUES)
            if issue_counts[code]
        ],
        "",
        "Issue counts:",
        *[f"- {code}: {total}" for code, total in issue_counts.most_common()],
        "",
        "Severity counts:",
        *[f"- {severity}: {total}" for severity, total in severity_counts.most_common()],
        "",
        "Top residue matches:",
        *[f"- {match}: {total}" for match, total in residue_counts.most_common(30)],
        "",
        "Top files:",
        *[f"- {path}: {total}" for path, total in file_counts.most_common(30)],
        "",
    ]

    for code, total in issue_counts.most_common():
        report_lines.extend([f"Samples: {code}", ""])
        for row, issue in samples[code]:
            report_lines.extend(
                [
                    f"- segment {row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']}",
                    f"  severity: {issue.get('severity')}",
                    f"  matches: {', '.join(str(item) for item in issue.get('matches') or [])}",
                    f"  EN: {short(row['english_text'])}",
                    f"  ES: {short(row['spanish_text'])}",
                    f"  TEXT: {short(candidate_text(row))}",
                ]
            )
        report_lines.append("")

    report_path = db.write_report(settings, "visual_residues", report_lines)
    print(f"[report_visual_residues] Revisable segments inspected: {inspected}")
    print(f"[report_visual_residues] Segments with issues: {len(affected_segments)}")
    print(f"[report_visual_residues] Segments with actionable issues: {len(actionable_segments)}")
    for code, total in issue_counts.most_common():
        print(f"[report_visual_residues] {code}: {total}")
    print(f"[report_visual_residues] Report: {report_path}")
    print("[report_visual_residues] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Report Spanish residue and visual text issues in revisable segments.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum samples per issue code.")
    args = parser.parse_args()
    main(limit=args.limit)
