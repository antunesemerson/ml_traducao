from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import domain_policy_vote_candidate_low_plain_remaining_protected_apply_dry_run as dry


APPLY_SUMMARY_PATH = Path("reports/20260629_112301_087326_domain_policy_vote_candidate_low_plain_remaining_protected_apply_apply_summary.json")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_summary() -> dict[str, Any]:
    return json.loads(APPLY_SUMMARY_PATH.read_text(encoding="utf-8"))


def validate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with dry.connect_readonly() as conn:
        state_by_id = dry.fetch_state(conn, [int(record["segment_id"]) for record in records])
    output: list[dict[str, Any]] = []
    for record in records:
        segment_id = int(record["segment_id"])
        state = state_by_id.get(segment_id) or {}
        reasons: list[str] = []
        db_output = str(state.get("output_text") or "")
        path = Path(record["output_path"])
        line_number = int(record["output_line_number"])
        disk_line = ""
        if db_output != record["corrected_text"]:
            reasons.append("database_output_not_corrected_text")
        if not path.exists():
            reasons.append("missing_output_file")
        elif line_number <= 0:
            reasons.append("missing_output_line_number")
        else:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            index = line_number - 1
            if index < 0 or index >= len(lines):
                reasons.append("line_out_of_range")
            else:
                disk_line = lines[index]
                if disk_line != record["new_line"]:
                    reasons.append("disk_line_not_expected_new_line")
        output.append(
            {
                "segment_id": segment_id,
                "status": "ok" if not reasons else "failed",
                "reasons": reasons,
                "relative_path": record["relative_path"],
                "source_key": record["source_key"],
                "output_line_number": line_number,
                "corrected_text": record["corrected_text"],
                "database_output_text": db_output,
                "disk_line": disk_line,
            }
        )
    return output


def write_reports(post_validation: list[dict[str, Any]], source_summary: dict[str, Any]) -> tuple[Path, Path]:
    ok_count = sum(1 for record in post_validation if record["status"] == "ok")
    failed_count = sum(1 for record in post_validation if record["status"] != "ok")
    summary = {
        "created_at": now(),
        "mode": "read_only_post_validation",
        "source_apply_summary": str(APPLY_SUMMARY_PATH),
        "record_count": len(post_validation),
        "post_validation_ok_count": ok_count,
        "post_validation_failed_count": failed_count,
        "source_changed": source_summary.get("source_changed"),
        "output_changed": source_summary.get("output_changed"),
        "database_output_segments_changed": source_summary.get("database_output_segments_changed"),
        "backup_paths": source_summary.get("backup_paths") or {},
        "rollback_paths": source_summary.get("rollback_paths") or {},
        "gates": {
            "apply": "not_run",
            "lifecycle": "not_run",
            "segment_state": "not_run",
            "reindex": "not_run",
            "full_production": "not_run",
        },
        "post_validation": post_validation,
        "output_files": {},
    }
    base = dry.reports_dir() / f"{stamp()}_domain_policy_vote_candidate_low_plain_remaining_apply_post_validation"
    summary_path = Path(str(base) + "_summary.json")
    txt_path = base.with_suffix(".txt")
    summary["output_files"] = {
        "summary_json": str(summary_path),
        "txt": str(txt_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain_policy_vote_candidate low_plain remaining apply post-validation",
        "",
        f"source_apply_summary: {APPLY_SUMMARY_PATH}",
        f"record_count: {len(post_validation)}",
        f"post_validation_ok_count: {ok_count}",
        f"post_validation_failed_count: {failed_count}",
        "",
        "items:",
    ]
    for record in post_validation:
        lines.extend(
            [
                "",
                f"## segment_id {record['segment_id']} | status={record['status']}",
                f"- key: {record['source_key']}",
                f"- reasons: {record['reasons']}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, summary_path


def main() -> None:
    source_summary = load_summary()
    post_validation = validate(source_summary["records"])
    txt_path, summary_path = write_reports(post_validation, source_summary)
    print(f"txt={txt_path}")
    print(f"summary={summary_path}")
    print(f"record_count={len(post_validation)}")
    print(f"post_validation_ok_count={sum(1 for record in post_validation if record['status'] == 'ok')}")
    print(f"post_validation_failed_count={sum(1 for record in post_validation if record['status'] != 'ok')}")


if __name__ == "__main__":
    main()
