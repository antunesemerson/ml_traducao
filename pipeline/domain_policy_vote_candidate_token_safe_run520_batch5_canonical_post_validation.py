from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import db


SEGMENT_IDS = [51754, 55794, 56160, 56498]
SOURCE = "domain_policy_vote_candidate_token_safe_run520_batch5_canonical_post_validation_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def disk_contains_text(line: str, text: str) -> bool:
    escaped = text.replace('"', r'\"')
    return text in line or escaped in line


def main() -> None:
    conn = db.connect()
    rows = conn.execute(
        """
        SELECT
            os.segment_id,
            os.relative_path,
            s.source_key,
            os.output_line_number,
            os.portuguese_text AS output_text,
            c.confirmed_text
        FROM output_segments os
        JOIN source_segments s ON s.id = os.segment_id
        JOIN segment_confirmations c ON c.segment_id = os.segment_id
        WHERE os.segment_id IN ({})
        ORDER BY os.segment_id
        """.format(",".join("?" for _ in SEGMENT_IDS)),
        SEGMENT_IDS,
    ).fetchall()
    conn.close()

    records = []
    for row in rows:
        relative_path = row["relative_path"]
        output_path = db.project_path(db.load_settings()["output_spanish"]) / relative_path
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        line_number = int(row["output_line_number"])
        disk_line = lines[line_number - 1] if 0 < line_number <= len(lines) else ""
        db_matches = row["output_text"] == row["confirmed_text"]
        disk_matches_canonical = disk_contains_text(disk_line, row["confirmed_text"])
        status = "ok" if db_matches and disk_matches_canonical else "failed"
        records.append(
            {
                "segment_id": row["segment_id"],
                "relative_path": relative_path,
                "source_key": row["source_key"],
                "output_line_number": line_number,
                "confirmed_text": row["confirmed_text"],
                "database_output_text": row["output_text"],
                "disk_line": disk_line,
                "database_matches_confirmed_text": db_matches,
                "disk_matches_confirmed_text_or_yaml_escaped_text": disk_matches_canonical,
                "status": status,
            }
        )

    status_counts: dict[str, int] = {}
    for record in records:
        status_counts[record["status"]] = status_counts.get(record["status"], 0) + 1

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "canonical_post_validation_read_only",
        "segment_ids": SEGMENT_IDS,
        "record_count": len(records),
        "ok_count": status_counts.get("ok", 0),
        "failed_count": status_counts.get("failed", 0),
        "status_counts": status_counts,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "records": records,
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_token_safe_run520_batch5_canonical_post_validation"
    json_path = base.with_suffix(".json")
    txt_path = base.with_suffix(".txt")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
