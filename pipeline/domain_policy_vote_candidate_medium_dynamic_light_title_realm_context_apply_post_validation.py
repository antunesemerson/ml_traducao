from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import escape_localization_value


APPLY_SUMMARY_PATH = Path("reports/20260629_151004_283326_domain_policy_vote_candidate_medium_dynamic_light_protected_apply_apply_summary.json")
SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_title_realm_context_apply_post_validation"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_record(record: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    path = Path(record["output_path"])
    line_number = int(record["output_line_number"])
    corrected = str(record["corrected_text"])
    expected_line = str(record["new_line"])
    disk_line = ""
    if not path.exists():
        reasons.append("missing_output_file")
    else:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        index = line_number - 1
        if index < 0 or index >= len(lines):
            reasons.append("line_out_of_range")
        else:
            disk_line = lines[index]
            if disk_line != expected_line:
                reasons.append("disk_line_not_expected_new_line")
            escaped = escape_localization_value(corrected)
            if corrected not in disk_line and escaped not in disk_line:
                reasons.append("disk_line_does_not_contain_corrected_text_or_escaped_equivalent")
    return {
        "segment_id": int(record["segment_id"]),
        "status": "ok" if not reasons else "failed",
        "reasons": reasons,
        "relative_path": record["relative_path"],
        "source_key": record["source_key"],
        "output_line_number": line_number,
        "corrected_text": corrected,
        "escaped_corrected_text": escape_localization_value(corrected),
        "disk_line": disk_line,
    }


def main() -> None:
    payload = json.loads(APPLY_SUMMARY_PATH.read_text(encoding="utf-8"))
    records = payload["records"]
    validations = [validate_record(record) for record in records]
    failed = [record for record in validations if record["status"] != "ok"]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "apply_summary_path": str(APPLY_SUMMARY_PATH),
        "validated_count": len(validations),
        "post_validation_ok_count": len(validations) - len(failed),
        "post_validation_failed_count": len(failed),
        "post_validation": validations,
        "source_changed": False,
        "output_changed": True,
        "database_output_segments_changed": True,
        "production_full_recommended_now": False,
        "output_files": {},
    }
    base = reports_dir() / f"{stamp()}_{SOURCE}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in validations:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light title realm context apply post-validation",
        f"validated_count: {summary['validated_count']}",
        f"post_validation_ok_count: {summary['post_validation_ok_count']}",
        f"post_validation_failed_count: {summary['post_validation_failed_count']}",
    ]
    for record in validations:
        lines.extend(["", f"## segment_id {record['segment_id']} | {record['status']}", f"- reasons: {record['reasons']}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
