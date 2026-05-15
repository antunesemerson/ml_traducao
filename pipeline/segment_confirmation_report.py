from __future__ import annotations

from datetime import datetime

import db


RULE_VERSION = "segment_confirmation_report_v2"


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
        package_stats = conn.execute(
            """
            WITH package_totals AS (
                SELECT
                    relative_path,
                    COUNT(*) AS total_segments,
                    SUM(CASE WHEN sc.segment_id IS NOT NULL THEN 1 ELSE 0 END) AS confirmed_segments,
                    SUM(
                        CASE
                            WHEN sc.confirmation_level = 'human_confirmed'
                             AND sc.locked = 1
                            THEN 1
                            ELSE 0
                        END
                    ) AS human_locked_segments
                FROM source_segments ss
                LEFT JOIN segment_confirmations sc ON sc.segment_id = ss.id
                WHERE ss.is_active = 1
                GROUP BY relative_path
            )
            SELECT
                COUNT(*) AS total_packages,
                SUM(CASE WHEN confirmed_segments = total_segments THEN 1 ELSE 0 END) AS resolved_packages,
                SUM(CASE WHEN human_locked_segments = total_segments THEN 1 ELSE 0 END) AS human_locked_packages,
                SUM(CASE WHEN confirmed_segments < total_segments THEN 1 ELSE 0 END) AS pending_packages
            FROM package_totals
            """
        ).fetchone()
        pending_packages = conn.execute(
            """
            WITH package_totals AS (
                SELECT
                    ss.relative_path,
                    COUNT(*) AS total_segments,
                    SUM(CASE WHEN sc.segment_id IS NOT NULL THEN 1 ELSE 0 END) AS confirmed_segments
                FROM source_segments ss
                LEFT JOIN segment_confirmations sc ON sc.segment_id = ss.id
                WHERE ss.is_active = 1
                GROUP BY ss.relative_path
            )
            SELECT
                relative_path,
                total_segments,
                confirmed_segments,
                total_segments - confirmed_segments AS pending_segments
            FROM package_totals
            WHERE confirmed_segments < total_segments
            ORDER BY pending_segments ASC, relative_path
            LIMIT 15
            """
        ).fetchall()
        focus_stats = conn.execute(
            """
            SELECT
                focus_group,
                COUNT(*) AS total_packages,
                SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) AS resolved_packages,
                SUM(CASE WHEN status <> 'resolved' THEN 1 ELSE 0 END) AS pending_packages,
                SUM(total_segments) AS total_segments,
                SUM(confirmed_segments) AS confirmed_segments,
                SUM(pending_segments) AS pending_segments
            FROM package_focus_queue
            WHERE focus_group = 'high_impact_v1'
            GROUP BY focus_group
            """
        ).fetchone()

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
    total_packages = int(package_stats["total_packages"] or 0)
    resolved_packages = int(package_stats["resolved_packages"] or 0)
    human_locked_packages = int(package_stats["human_locked_packages"] or 0)
    package_pending = int(package_stats["pending_packages"] or 0)
    focus_total = int(focus_stats["total_packages"] or 0) if focus_stats else 0
    focus_resolved = int(focus_stats["resolved_packages"] or 0) if focus_stats else 0
    focus_pending = int(focus_stats["pending_packages"] or 0) if focus_stats else 0
    focus_total_segments = int(focus_stats["total_segments"] or 0) if focus_stats else 0
    focus_confirmed_segments = int(focus_stats["confirmed_segments"] or 0) if focus_stats else 0
    focus_pending_segments = int(focus_stats["pending_segments"] or 0) if focus_stats else 0
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
        "Package coverage:",
        f"- Active packages: {total_packages}",
        f"- Closed packages: {human_locked_packages} / {total_packages} ({percent(human_locked_packages, total_packages):.4f}%)",
        f"- Resolved packages: {resolved_packages} ({percent(resolved_packages, total_packages):.4f}%)",
        f"- Human locked packages: {human_locked_packages} ({percent(human_locked_packages, total_packages):.4f}%)",
        f"- Packages with pending segments: {package_pending}",
        "",
        "High-impact focus:",
        f"- Focus group: high_impact_v1",
        f"- Focus packages closed: {focus_resolved} / {focus_total} ({percent(focus_resolved, focus_total):.4f}%)",
        f"- Focus packages pending: {focus_pending}",
        f"- Focus segments confirmed: {focus_confirmed_segments} / {focus_total_segments} ({percent(focus_confirmed_segments, focus_total_segments):.4f}%)",
        f"- Focus segments pending: {focus_pending_segments}",
        "",
        "Smallest pending packages:",
        *[
            (
                f"- {row['pending_segments']} pending / {row['total_segments']} total | "
                f"{row['relative_path']}"
            )
            for row in pending_packages
        ],
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
    print(
        "[segment_confirmation_report] Closed packages: "
        f"{human_locked_packages}/{total_packages} ({percent(human_locked_packages, total_packages):.4f}%)"
    )
    print(f"[segment_confirmation_report] Human confirmed: {human_confirmed}")
    print(f"[segment_confirmation_report] Auto confirmed: {auto_confirmed}")
    print(f"[segment_confirmation_report] Human locked: {locked}")
    print(
        "[segment_confirmation_report] Packages resolved: "
        f"{resolved_packages}/{total_packages} ({percent(resolved_packages, total_packages):.4f}%)"
    )
    print(f"[segment_confirmation_report] Human locked packages: {human_locked_packages}")
    print(f"[segment_confirmation_report] Packages with pending segments: {package_pending}")
    print(
        "[segment_confirmation_report] High-impact packages closed: "
        f"{focus_resolved}/{focus_total} ({percent(focus_resolved, focus_total):.4f}%)"
    )
    print(f"[segment_confirmation_report] Report: {report_path}")
    print("[segment_confirmation_report] Done")


if __name__ == "__main__":
    main()
