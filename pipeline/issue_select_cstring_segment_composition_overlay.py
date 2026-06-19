from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_select_cstring_segment_composition_overlay_v1"
OVERLAY_NAME = "select_cstring_segment_composition_overlay_v1"
OVERLAY_STATUS = "shadow"
OVERLAY_ACTION = "measure_segment_level_select_cstring_composition"
PRODUCTION_RELEASE_ALLOWED = 0


def latest_id_for(conn, table: str, where: str = "finished_at IS NOT NULL") -> int | None:
    row = conn.execute(f"SELECT id FROM {table} WHERE {where} ORDER BY id DESC LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_segment_composition_overlay_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            rule_version TEXT NOT NULL,
            overlay_name TEXT NOT NULL,
            overlay_status TEXT NOT NULL,
            overlay_action TEXT NOT NULL,
            source_final_overlay_run_id INTEGER,
            source_preterite_checkpoint_run_id INTEGER,
            segment_count INTEGER NOT NULL DEFAULT 0,
            final_overlay_segment_count INTEGER NOT NULL DEFAULT 0,
            preterite_segment_count INTEGER NOT NULL DEFAULT 0,
            multi_source_segment_count INTEGER NOT NULL DEFAULT 0,
            composed_allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            source_counts_json TEXT,
            block_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_segment_composition_overlay_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            source_final_overlay_run_id INTEGER,
            source_final_overlay_item_id INTEGER,
            source_preterite_checkpoint_run_id INTEGER,
            source_preterite_checkpoint_item_id INTEGER,
            final_overlay_allowed INTEGER NOT NULL DEFAULT 0,
            preterite_checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            composed_allowed INTEGER NOT NULL DEFAULT 0,
            composition_source TEXT NOT NULL,
            composition_action TEXT NOT NULL,
            block_reason TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            reasons_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_segment_composition_overlay_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_segment_composition_items_run
        ON ml_issue_select_cstring_segment_composition_overlay_items(run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_segment_composition_items_segment
        ON ml_issue_select_cstring_segment_composition_overlay_items(segment_id)
        """
    )


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_select_cstring_segment_composition_overlay"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_final_overlay_segments(conn, *, run_id: int | None) -> dict[int, dict[str, Any]]:
    if run_id is None:
        return {}
    rows = conn.execute(
        """
        SELECT id AS source_final_overlay_item_id, segment_id, relative_path, source_key
        FROM ml_issue_select_cstring_final_composition_overlay_items
        WHERE run_id = ?
          AND composition_allowed = 1
          AND COALESCE(block_reason, '') = ''
          AND production_release_allowed = 0
        ORDER BY segment_id, id
        """,
        (run_id,),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["segment_id"])].append(dict(row))
    return {segment_id: items[0] for segment_id, items in grouped.items() if len(items) == 1}


def fetch_preterite_segments(conn, *, run_id: int | None) -> dict[int, dict[str, Any]]:
    if run_id is None:
        return {}
    rows = conn.execute(
        """
        SELECT
            item.id AS source_preterite_checkpoint_item_id,
            item.segment_id,
            src.relative_path,
            src.source_key
        FROM ml_issue_select_cstring_preterite_ptbr_checkpoint_items item
        LEFT JOIN source_segments src ON src.id = item.segment_id
        WHERE item.run_id = ?
          AND item.checkpoint_allowed = 1
          AND COALESCE(item.block_reason, '') = ''
          AND item.production_release_allowed = 0
        ORDER BY item.segment_id, item.id
        """,
        (run_id,),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["segment_id"] is None:
            continue
        grouped[int(row["segment_id"])].append(dict(row))
    return {segment_id: items[0] for segment_id, items in grouped.items() if len(items) == 1}


def compose_rows(
    *,
    final_segments: dict[int, dict[str, Any]],
    preterite_segments: dict[int, dict[str, Any]],
    final_run_id: int | None,
    preterite_run_id: int | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for segment_id in sorted(set(final_segments) | set(preterite_segments)):
        final_item = final_segments.get(segment_id)
        preterite_item = preterite_segments.get(segment_id)
        reasons: list[str] = []
        final_allowed = 1 if final_item else 0
        preterite_allowed = 1 if preterite_item else 0
        if final_item and preterite_item:
            composition_source = "final_overlay+preterite_checkpoint"
            reasons.extend(["covered_by_final_overlay", "covered_by_preterite_checkpoint"])
        elif final_item:
            composition_source = "final_overlay"
            reasons.append("covered_by_final_overlay")
        elif preterite_item:
            composition_source = "preterite_checkpoint"
            reasons.append("covered_by_preterite_checkpoint")
        else:
            composition_source = "blocked"
            reasons.append("no_segment_composition_source")
        composed_allowed = 1 if (final_allowed or preterite_allowed) else 0
        block_reason = "" if composed_allowed else "no_segment_composition_source"
        context = final_item or preterite_item or {}
        rows.append(
            {
                "segment_id": segment_id,
                "relative_path": context.get("relative_path"),
                "source_key": context.get("source_key"),
                "source_final_overlay_run_id": final_run_id,
                "source_final_overlay_item_id": final_item.get("source_final_overlay_item_id") if final_item else None,
                "source_preterite_checkpoint_run_id": preterite_run_id,
                "source_preterite_checkpoint_item_id": preterite_item.get("source_preterite_checkpoint_item_id") if preterite_item else None,
                "final_overlay_allowed": final_allowed,
                "preterite_checkpoint_allowed": preterite_allowed,
                "composed_allowed": composed_allowed,
                "composition_source": composition_source,
                "composition_action": OVERLAY_ACTION,
                "block_reason": block_reason,
                "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
                "reasons": reasons,
            }
        )
    return rows


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    final_run_id: int | None,
    preterite_run_id: int | None,
    rows: list[dict[str, Any]],
    source_counts: Counter[str],
    block_counts: Counter[str],
) -> None:
    fields = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_final_overlay_item_id",
        "source_preterite_checkpoint_item_id",
        "final_overlay_allowed",
        "preterite_checkpoint_allowed",
        "composed_allowed",
        "composition_source",
        "block_reason",
        "reasons",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            payload = dict(row)
            payload["reasons"] = "; ".join(row["reasons"])
            writer.writerow(payload)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {"run_id": run_id, **row}
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    allowed = sum(1 for row in rows if int(row["composed_allowed"] or 0) == 1)
    blocked = len(rows) - allowed
    lines = [
        "Select_CString segment composition overlay",
        f"Rule version: {RULE_VERSION}",
        f"Overlay name: {OVERLAY_NAME}",
        f"Overlay status: {OVERLAY_STATUS}",
        f"Overlay run id: {run_id}",
        f"Source final overlay run id: {final_run_id or ''}",
        f"Source preterite checkpoint run id: {preterite_run_id or ''}",
        f"Production release allowed: {PRODUCTION_RELEASE_ALLOWED}",
        "",
        "Summary:",
        f"- Segments composed: {len(rows):,}",
        f"- Final overlay segments: {source_counts.get('final_overlay', 0):,}",
        f"- Preterite checkpoint segments: {source_counts.get('preterite_checkpoint', 0):,}",
        f"- Multi-source segments: {source_counts.get('final_overlay+preterite_checkpoint', 0):,}",
        f"- Composed allowed: {allowed:,}",
        f"- Blocked: {blocked:,}",
        "",
        "Composition sources:",
        *[f"- {key}: {value:,}" for key, value in source_counts.most_common()],
        "",
        "Blocks:",
        *([f"- {key}: {value:,}" for key, value in block_counts.most_common()] or ["- none"]),
        "",
        "Safety:",
        "- Shadow-only segment composition overlay.",
        "- It reads SQLite evidence only; it does not read source files, write output, or release production authority.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(*, final_overlay_run_id: int | None = None, preterite_checkpoint_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    now = datetime.now().isoformat(timespec="seconds")
    txt_path, csv_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        ensure_tables(conn)
        selected_final_run_id = final_overlay_run_id
        if selected_final_run_id is None:
            selected_final_run_id = latest_id_for(conn, "ml_issue_select_cstring_final_composition_overlay_runs")
        selected_preterite_run_id = preterite_checkpoint_run_id
        if selected_preterite_run_id is None:
            selected_preterite_run_id = latest_id_for(conn, "ml_issue_select_cstring_preterite_ptbr_checkpoint_runs")
        final_segments = fetch_final_overlay_segments(conn, run_id=selected_final_run_id)
        preterite_segments = fetch_preterite_segments(conn, run_id=selected_preterite_run_id)
        rows = compose_rows(
            final_segments=final_segments,
            preterite_segments=preterite_segments,
            final_run_id=selected_final_run_id,
            preterite_run_id=selected_preterite_run_id,
        )
        source_counts = Counter(row["composition_source"] for row in rows)
        block_counts = Counter(row["block_reason"] for row in rows if not int(row["composed_allowed"] or 0))
        allowed = sum(1 for row in rows if int(row["composed_allowed"] or 0) == 1)
        blocked = len(rows) - allowed
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_segment_composition_overlay_runs (
                started_at, finished_at, rule_version, overlay_name, overlay_status,
                overlay_action, source_final_overlay_run_id, source_preterite_checkpoint_run_id,
                segment_count, final_overlay_segment_count, preterite_segment_count,
                multi_source_segment_count, composed_allowed_count, blocked_count,
                production_release_allowed, source_counts_json, block_counts_json,
                report_path, csv_path, jsonl_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                RULE_VERSION,
                OVERLAY_NAME,
                OVERLAY_STATUS,
                OVERLAY_ACTION,
                selected_final_run_id,
                selected_preterite_run_id,
                len(rows),
                source_counts.get("final_overlay", 0),
                source_counts.get("preterite_checkpoint", 0),
                source_counts.get("final_overlay+preterite_checkpoint", 0),
                allowed,
                blocked,
                json.dumps(dict(source_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_select_cstring_segment_composition_overlay_items (
                run_id, segment_id, relative_path, source_key,
                source_final_overlay_run_id, source_final_overlay_item_id,
                source_preterite_checkpoint_run_id, source_preterite_checkpoint_item_id,
                final_overlay_allowed, preterite_checkpoint_allowed, composed_allowed,
                composition_source, composition_action, block_reason, production_release_allowed,
                reasons_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            [
                (
                    run_id,
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_final_overlay_run_id"],
                    row["source_final_overlay_item_id"],
                    row["source_preterite_checkpoint_run_id"],
                    row["source_preterite_checkpoint_item_id"],
                    row["final_overlay_allowed"],
                    row["preterite_checkpoint_allowed"],
                    row["composed_allowed"],
                    row["composition_source"],
                    row["composition_action"],
                    row["block_reason"],
                    json.dumps(row["reasons"], ensure_ascii=False, sort_keys=True),
                    now,
                )
                for row in rows
            ],
        )
        conn.commit()
    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        run_id=run_id,
        final_run_id=selected_final_run_id,
        preterite_run_id=selected_preterite_run_id,
        rows=rows,
        source_counts=source_counts,
        block_counts=block_counts,
    )
    payload = {
        "run_id": run_id,
        "source_final_overlay_run_id": selected_final_run_id,
        "source_preterite_checkpoint_run_id": selected_preterite_run_id,
        "segment_count": len(rows),
        "composed_allowed_count": allowed,
        "blocked_count": blocked,
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
        "report_path": str(txt_path),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Measure Select_CString segment-level composition across shadow checkpoints.")
    parser.add_argument("--source-final-overlay-run-id", type=int, default=None)
    parser.add_argument("--source-preterite-checkpoint-run-id", type=int, default=None)
    args = parser.parse_args()
    run(
        final_overlay_run_id=args.source_final_overlay_run_id,
        preterite_checkpoint_run_id=args.source_preterite_checkpoint_run_id,
    )
