from __future__ import annotations

import argparse
from datetime import datetime

import db
import package_priority_report


RULE_VERSION = "build_package_focus_queue_v1"
DEFAULT_GROUP = "high_impact_v1"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def package_metrics(conn) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT
            s.relative_path,
            COUNT(*) AS total_segments,
            SUM(CASE WHEN sc.segment_id IS NOT NULL THEN 1 ELSE 0 END) AS confirmed_segments,
            SUM(CASE WHEN sc.confirmation_level = 'human_confirmed' THEN 1 ELSE 0 END) AS human_confirmed_segments,
            SUM(CASE WHEN sc.confirmation_level = 'auto_confirmed' THEN 1 ELSE 0 END) AS auto_confirmed_segments
        FROM source_segments s
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
        GROUP BY s.relative_path
        """
    ).fetchall()
    metrics: dict[str, dict] = {}
    for row in rows:
        item = dict(row)
        item["pending_segments"] = int(item["total_segments"] or 0) - int(item["confirmed_segments"] or 0)
        item["status"] = "resolved" if item["pending_segments"] == 0 else "pending"
        metrics[item["relative_path"]] = item
    return metrics


def current_focus_paths(conn, focus_group: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT relative_path
        FROM package_focus_queue
        WHERE focus_group = ?
        """,
        (focus_group,),
    ).fetchall()
    return {row["relative_path"] for row in rows}


def ranked_pending_packages(conn, inspect_limit: int | None, sample_limit: int) -> list[dict]:
    packages = package_priority_report.fetch_pending_packages(conn, inspect_limit)
    ranked: list[dict] = []
    for package in packages:
        residue_hits, hits = package_priority_report.residue_summary(
            conn,
            package["relative_path"],
            sample_limit,
        )
        package["residue_hits"] = residue_hits
        package["term_hits"] = hits
        package["score"] = package_priority_report.priority_score(
            package["relative_path"],
            package["pending_segments"],
            package["total_segments"],
            residue_hits,
        )
        ranked.append(package)
    ranked.sort(key=lambda item: (item["score"], item["pending_segments"], item["relative_path"]), reverse=True)
    return ranked


def upsert_focus_rows(conn, focus_group: str, rows: list[dict], timestamp: str) -> int:
    inserted = 0
    for row in rows:
        existing = conn.execute(
            """
            SELECT id
            FROM package_focus_queue
            WHERE focus_group = ?
              AND relative_path = ?
            """,
            (focus_group, row["relative_path"]),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO package_focus_queue (
                focus_group,
                relative_path,
                priority_score,
                total_segments,
                confirmed_segments,
                pending_segments,
                human_confirmed_segments,
                auto_confirmed_segments,
                status,
                reason,
                first_seen_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(focus_group, relative_path) DO UPDATE SET
                priority_score = excluded.priority_score,
                total_segments = excluded.total_segments,
                confirmed_segments = excluded.confirmed_segments,
                pending_segments = excluded.pending_segments,
                human_confirmed_segments = excluded.human_confirmed_segments,
                auto_confirmed_segments = excluded.auto_confirmed_segments,
                status = excluded.status,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (
                focus_group,
                row["relative_path"],
                row["score"],
                row["total_segments"],
                row["confirmed_segments"],
                row["pending_segments"],
                row["human_confirmed"],
                row["auto_confirmed"],
                "resolved" if row["pending_segments"] == 0 else "pending",
                row.get("reason") or f"Selected by {RULE_VERSION}",
                timestamp,
                timestamp,
            ),
        )
        if existing is None:
            inserted += 1
    return inserted


def refresh_focus_rows(conn, focus_group: str, metrics: dict[str, dict], timestamp: str) -> None:
    rows = conn.execute(
        """
        SELECT relative_path
        FROM package_focus_queue
        WHERE focus_group = ?
        """,
        (focus_group,),
    ).fetchall()
    for row in rows:
        metric = metrics.get(row["relative_path"])
        if metric is None:
            continue
        conn.execute(
            """
            UPDATE package_focus_queue
            SET total_segments = ?,
                confirmed_segments = ?,
                pending_segments = ?,
                human_confirmed_segments = ?,
                auto_confirmed_segments = ?,
                status = ?,
                updated_at = ?
            WHERE focus_group = ?
              AND relative_path = ?
            """,
            (
                metric["total_segments"],
                metric["confirmed_segments"],
                metric["pending_segments"],
                metric["human_confirmed_segments"],
                metric["auto_confirmed_segments"],
                metric["status"],
                timestamp,
                focus_group,
                row["relative_path"],
            ),
        )


def focus_summary(conn, focus_group: str) -> dict:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) AS resolved,
            SUM(CASE WHEN status <> 'resolved' THEN 1 ELSE 0 END) AS pending,
            SUM(total_segments) AS total_segments,
            SUM(confirmed_segments) AS confirmed_segments,
            SUM(pending_segments) AS pending_segments
        FROM package_focus_queue
        WHERE focus_group = ?
        """,
        (focus_group,),
    ).fetchone()
    return dict(row)


def main(
    focus_group: str = DEFAULT_GROUP,
    focus_limit: int = 100,
    inspect_limit: int | None = 300,
    sample_limit: int = 120,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    timestamp = now()

    print("[build_package_focus_queue] Starting package focus queue")
    print(f"[build_package_focus_queue] Rule version: {RULE_VERSION}")
    print(f"[build_package_focus_queue] Focus group: {focus_group}")
    print(f"[build_package_focus_queue] Focus limit: {focus_limit}")
    print(f"[build_package_focus_queue] Inspect limit: {inspect_limit or 'none'}")
    print(f"[build_package_focus_queue] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        existing_paths = current_focus_paths(conn, focus_group)
        slots = max(focus_limit - len(existing_paths), 0)
        ranked = ranked_pending_packages(conn, inspect_limit, sample_limit) if slots else []
        selected = [item for item in ranked if item["relative_path"] not in existing_paths][:slots]
        inserted = upsert_focus_rows(conn, focus_group, selected, timestamp) if selected else 0
        refresh_focus_rows(conn, focus_group, package_metrics(conn), timestamp)
        conn.commit()
        summary = focus_summary(conn, focus_group)
        next_targets = conn.execute(
            """
            SELECT relative_path, priority_score, pending_segments, total_segments, confirmed_segments
            FROM package_focus_queue
            WHERE focus_group = ?
              AND status <> 'resolved'
            ORDER BY priority_score DESC, pending_segments DESC, relative_path
            LIMIT 25
            """,
            (focus_group,),
        ).fetchall()

    total = int(summary["total"] or 0)
    resolved = int(summary["resolved"] or 0)
    pending = int(summary["pending"] or 0)
    pct = resolved / total * 100 if total else 0
    elapsed = datetime.now() - started_at
    report_lines = [
        "Package focus queue report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Focus group: {focus_group}",
        f"Focus limit: {focus_limit}",
        f"Inspect limit: {inspect_limit or 'none'}",
        "",
        "Summary:",
        f"- Focus packages resolved: {resolved} / {total} ({pct:.4f}%)",
        f"- Focus packages pending: {pending}",
        f"- Focus segments confirmed: {summary['confirmed_segments'] or 0} / {summary['total_segments'] or 0}",
        f"- Focus segments pending: {summary['pending_segments'] or 0}",
        f"- Newly inserted packages: {inserted}",
        "",
        "Next focus targets:",
        *[
            (
                f"- score={row['priority_score']:.1f} | pending={row['pending_segments']} / "
                f"{row['total_segments']} | {row['relative_path']}"
            )
            for row in next_targets
        ],
    ]
    report_path = db.write_report(settings, "package_focus_queue", report_lines)
    print(f"[build_package_focus_queue] Focus packages resolved: {resolved}/{total} ({pct:.4f}%)")
    print(f"[build_package_focus_queue] Focus packages pending: {pending}")
    print(f"[build_package_focus_queue] Newly inserted packages: {inserted}")
    print(f"[build_package_focus_queue] Report: {report_path}")
    print("[build_package_focus_queue] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build or refresh the high-impact package focus queue.")
    parser.add_argument("--focus-group", default=DEFAULT_GROUP, help="Focus queue name.")
    parser.add_argument("--focus-limit", type=int, default=100, help="Maximum packages in the focus queue.")
    parser.add_argument("--inspect-limit", type=int, default=300, help="Maximum pending packages to rank.")
    parser.add_argument("--sample-limit", type=int, default=120, help="Pending rows sampled per package.")
    args = parser.parse_args()
    main(
        focus_group=args.focus_group,
        focus_limit=args.focus_limit,
        inspect_limit=args.inspect_limit,
        sample_limit=args.sample_limit,
    )
