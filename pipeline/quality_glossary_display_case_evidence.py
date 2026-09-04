from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter

import db
import quality_shadow_store
from quality_glossary_display_case_shadow import (
    REVIEW_LANE,
    RULE_VERSION as SOURCE_RULE_VERSION,
)


RULE_VERSION = "quality_glossary_display_case_evidence_v1"
EVIDENCE_TYPE = "glossary_display_case_loss_review"


def reconcile_inactive_evidence(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE ml_pairwise_quality_evidence
        SET training_eligible = 0,
            promotion_eligible = 0
        WHERE evidence_type = ?
        """,
        (EVIDENCE_TYPE,),
    )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Glossary display-case shadow without creating promotion evidence."
    )
    parser.add_argument("--shadow-run-id", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = db.load_settings()
    database_path = db.get_database_path(settings)

    if args.apply:
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            shadow_run, records = quality_shadow_store.load_snapshot(
                conn,
                source_rule_version=SOURCE_RULE_VERSION,
                run_id=args.shadow_run_id,
            )
            reconcile_inactive_evidence(conn)
    else:
        with sqlite3.connect(
            f"file:{database_path}?mode=ro",
            uri=True,
            timeout=300,
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            shadow_run, records = quality_shadow_store.load_snapshot(
                conn,
                source_rule_version=SOURCE_RULE_VERSION,
                run_id=args.shadow_run_id,
            )

    lane_counts = Counter(str(record.get("lane") or "") for record in records)
    payload = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "source_shadow_rule_version": SOURCE_RULE_VERSION,
        "shadow_run_id": int(shadow_run["id"]),
        "evidence_type": EVIDENCE_TYPE,
        "record_count": len(records),
        "review_required_count": int(lane_counts[REVIEW_LANE]),
        "lane_counts": dict(lane_counts),
        "evidence_count": 0,
        "promotion_ready_count": 0,
        "confirmation_write_count": 0,
        "output_write_count": 0,
        "output_changed": False,
        "shadow_only": True,
        "recommendation": (
            "Collect per-key human decisions before generating any pairwise repair evidence."
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
