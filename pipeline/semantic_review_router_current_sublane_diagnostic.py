from __future__ import annotations

import sqlite3

import db
import semantic_review_router_pending_deep_diagnostic as base


SOURCE = "semantic_review_router_current_sublane_diagnostic_v1"


def latest_segment_state_run_id() -> int:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    try:
        row = conn.execute("SELECT MAX(id) FROM segment_state_runs").fetchone()
    finally:
        conn.close()
    if not row or row[0] is None:
        raise SystemExit("missing segment_state_runs")
    return int(row[0])


def main() -> None:
    base.SOURCE = SOURCE
    base.SEGMENT_STATE_RUN_ID = latest_segment_state_run_id()
    base.main()


if __name__ == "__main__":
    main()
