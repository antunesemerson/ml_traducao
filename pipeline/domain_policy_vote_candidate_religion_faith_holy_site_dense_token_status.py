from __future__ import annotations

import json
import sqlite3
from collections import Counter

import db


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def main() -> None:
    with connect_readonly() as conn:
        latest_run = conn.execute(
            "SELECT id, finished_at FROM segment_state_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if latest_run is None:
            raise SystemExit("missing complete segment_state_run")
        run_id = int(latest_run["id"])
        global_counts = conn.execute(
            """
            SELECT
              SUM(CASE WHEN state_group = 'closed' THEN 1 ELSE 0 END) AS closed_count,
              SUM(CASE WHEN state_group = 'pending' THEN 1 ELSE 0 END) AS pending_count,
              SUM(CASE WHEN needs_output_apply = 1 THEN 1 ELSE 0 END) AS output_apply_pending_count
            FROM segment_state_items
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        final_state_counts = conn.execute(
            """
            SELECT final_state, COUNT(*) AS count
            FROM segment_state_items
            WHERE run_id = ? AND state_group = 'pending'
            GROUP BY final_state
            ORDER BY count DESC, final_state
            LIMIT 12
            """,
            (run_id,),
        ).fetchall()
        summary = {
            "segment_state_run_id": run_id,
            "segment_state_finished_at": latest_run["finished_at"],
            "global": dict(global_counts),
            "top_pending_final_states": [dict(row) for row in final_state_counts],
            "actions_run": Counter(
                {
                    "candidate_generation_count": 0,
                    "apply_count": 0,
                    "lifecycle_count": 0,
                    "segment_state_count": 0,
                    "reindex_count": 0,
                    "production_full_count": 0,
                }
            ),
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
