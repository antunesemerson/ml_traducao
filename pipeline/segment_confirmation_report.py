from __future__ import annotations

from datetime import datetime

import db


RULE_VERSION = "segment_confirmation_report_v1"


def percent(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return part / total * 100


def main() -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[segment_confirmation_report] Starting confirmation coverage report")
    print(f"[segment_confirmation_report] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        total_segments = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM source_segments
                WHERE is_active = 1
                """
            ).fetchone()[0]
            or 0
        )
        by_level = conn.execute(
            """
            SELECT confirmation_level, locked, COUNT(*) AS total
            FROM segment_confirmations
            GROUP BY confirmation_level, locked
            ORDER BY confirmation_level, locked
            """
        ).fetchall()
        by_source = conn.execute(
            """
            SELECT confirmation_source, confirmation_label, COUNT(*) AS total
            FROM segment_confirmations
            GROUP BY confirmation_source, confirmation_label
            ORDER BY total DESC, confirmation_source, confirmation_label
            """
        ).fetchall()
        recent = conn.execute(
            """
            SELECT
                sc.segment_id,
                sc.confirmation_level,
                sc.confirmation_label,
                sc.locked,
                sc.confidence_score,
                ss.relative_path,
                ss.source_key,
                sc.confirmed_at
            FROM segment_confirmations sc
            JOIN source_segments ss ON ss.id = sc.segment_id
            ORDER BY sc.updated_at DESC
            LIMIT 15
            """
        ).fetchall()

    human_confirmed = 0
    auto_confirmed = 0
    locked = 0
    for row in by_level:
        total = int(row["total"] or 0)
        if row["confirmation_level"] == "human_confirmed":
            human_confirmed += total
        elif row["confirmation_level"] == "auto_confirmed":
            auto_confirmed += total
        if int(row["locked"] or 0) == 1:
            locked += total

    total_confirmed = human_confirmed + auto_confirmed
    pending = max(total_segments - total_confirmed, 0)
    elapsed = datetime.now() - started_at

    report_lines = [
        "Segment confirmation coverage report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        "",
        "Coverage:",
        f"- Active segments: {total_segments}",
        f"- Human confirmed: {human_confirmed} ({percent(human_confirmed, total_segments):.4f}%)",
        f"- Auto confirmed: {auto_confirmed} ({percent(auto_confirmed, total_segments):.4f}%)",
        f"- Total confirmed: {total_confirmed} ({percent(total_confirmed, total_segments):.4f}%)",
        f"- Human locked: {locked}",
        f"- Pending confirmation: {pending}",
        "",
        "By level:",
        *[
            f"- {row['confirmation_level']} locked={row['locked']}: {row['total']}"
            for row in by_level
        ],
        "",
        "By source and label:",
        *[
            f"- {row['confirmation_source']} / {row['confirmation_label']}: {row['total']}"
            for row in by_source
        ],
        "",
        "Recent confirmations:",
        *[
            (
                f"- segment {row['segment_id']} | {row['confirmation_level']} | "
                f"{row['confirmation_label']} | locked={row['locked']} | "
                f"{row['relative_path']}::{row['source_key']}"
            )
            for row in recent
        ],
    ]
    if not by_level:
        report_lines.append("- No confirmations found")

    report_path = db.write_report(settings, "segment_confirmation_report", report_lines)
    print(
        "[segment_confirmation_report] Confirmed: "
        f"{total_confirmed}/{total_segments} ({percent(total_confirmed, total_segments):.4f}%)"
    )
    print(f"[segment_confirmation_report] Human confirmed: {human_confirmed}")
    print(f"[segment_confirmation_report] Auto confirmed: {auto_confirmed}")
    print(f"[segment_confirmation_report] Human locked: {locked}")
    print(f"[segment_confirmation_report] Report: {report_path}")
    print("[segment_confirmation_report] Done")


if __name__ == "__main__":
    main()
