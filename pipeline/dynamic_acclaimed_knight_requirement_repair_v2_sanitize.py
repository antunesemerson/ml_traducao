from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import structural_tokens


DECISION = "acclaimed_knight_requirement_repair_ready"
EXPECTED_LINE_CHANGES = 2
EXPECTED_CHANGED_IDS = {654, 661}
EXPECTED_CANDIDATES = 7

REPLACEMENTS = {
    654: (
        "O [acclaimed_knight|El] possui a tra\u00e7o [GetTrait('cautious_leader').GetName( GetNullCharacter )|l]",
        "O [acclaimed_knight|El] tem o tra\u00e7o [GetTrait('cautious_leader').GetName( GetNullCharacter )|l]",
    ),
    661: (
        "O [acclaimed_knight|El] possui a tra\u00e7o [GetTrait('flexible_leader').GetName( GetNullCharacter )|l]",
        "O [acclaimed_knight|El] tem o tra\u00e7o [GetTrait('flexible_leader').GetName( GetNullCharacter )|l]",
    ),
}

BAD_TEXT_PATTERNS = [
    "a tra\u00e7o",
    "a traco",
    "possui a tra\u00e7o",
    "possui a traco",
    "a traÃ§o",
    "possui a traÃ§o",
    "ÃƒÆ’",
    "Ãƒâ€š",
    "Ã¯Â¿Â½",
    "car\u00e1cter",
    "caracter",
    "combata",
    "ocupe",
    "ahora",
    "enamorado",
]
QUESTION_INSIDE_WORD_RE = re.compile(r"\w\?\w", re.UNICODE)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def output_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_acclaimed_knight_requirement_policy_reviewed_chat_v2"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def bad_text_reasons(value: str) -> list[str]:
    text = value.lower()
    reasons = [f"linguistic_guard:{pattern}" for pattern in BAD_TEXT_PATTERNS if pattern.lower() in text]
    if QUESTION_INSIDE_WORD_RE.search(value):
        reasons.append("linguistic_guard:question_inside_word")
    return reasons


def sanitize_rows(input_jsonl: Path) -> tuple[list[str], list[str], list[dict[str, Any]], set[int]]:
    original_lines = input_jsonl.read_text(encoding="utf-8").splitlines()
    output_lines: list[str] = []
    rows: list[dict[str, Any]] = []
    changed_ids: set[int] = set()
    errors: list[str] = []

    for line_number, line in enumerate(original_lines, start=1):
        row = json.loads(line)
        segment_id = int(row.get("segment_id") or 0)
        if segment_id in REPLACEMENTS:
            expected, replacement = REPLACEMENTS[segment_id]
            current_corrected = as_text(row.get("corrected_text"))
            if expected not in current_corrected:
                errors.append(f"expected_text_missing:{segment_id}:line_{line_number}")
                output_lines.append(line)
            else:
                row["corrected_text"] = current_corrected.replace(expected, replacement, 1)
                new_line = json.dumps(row, ensure_ascii=False, sort_keys=True)
                output_lines.append(new_line)
                changed_ids.add(segment_id)
        else:
            output_lines.append(line)
        rows.append(row)

    return original_lines, output_lines, rows, changed_ids, errors


def validate_v2(
    *,
    input_jsonl: Path,
    output_lines: list[str],
    original_lines: list[str],
    rows: list[dict[str, Any]],
    changed_ids: set[int],
) -> list[str]:
    errors: list[str] = []
    if len(output_lines) != len(original_lines):
        errors.append(f"line_count_changed:{len(original_lines)}->{len(output_lines)}")

    changed_line_numbers = [
        index + 1
        for index, (old, new) in enumerate(zip(original_lines, output_lines))
        if old != new
    ]
    if len(changed_line_numbers) != EXPECTED_LINE_CHANGES:
        errors.append(f"changed_line_count:{len(changed_line_numbers)}")
    if changed_ids != EXPECTED_CHANGED_IDS:
        errors.append(f"changed_ids:{sorted(changed_ids)}")

    candidates = [row for row in rows if row.get("decision") == DECISION]
    if len(candidates) != EXPECTED_CANDIDATES:
        errors.append(f"candidate_count:{len(candidates)}")

    for row in candidates:
        segment_id = int(row["segment_id"])
        corrected = as_text(row.get("corrected_text"))
        if not corrected:
            errors.append(f"corrected_text_empty:{segment_id}")
        reasons = bad_text_reasons(corrected)
        if reasons:
            errors.append(f"bad_corrected_text:{segment_id}:{';'.join(reasons)}")
        if row.get("tokens_preserved") is not True:
            errors.append(f"review_tokens_not_preserved:{segment_id}")
        if structural_tokens(as_text(row.get("current_text"))) != structural_tokens(corrected):
            errors.append(f"structural_tokens_changed:{segment_id}")

    for segment_id, (_, replacement) in REPLACEMENTS.items():
        row = next((item for item in candidates if int(item["segment_id"]) == segment_id), None)
        if row is None:
            errors.append(f"missing_candidate:{segment_id}")
        elif replacement not in as_text(row.get("corrected_text")):
            errors.append(f"replacement_missing:{segment_id}")

    if not input_jsonl.exists():
        errors.append(f"input_missing:{input_jsonl}")
    return errors


def run(*, input_jsonl: Path) -> tuple[Path, Path, dict[str, Any]]:
    settings = db.load_settings()
    output_jsonl, output_txt = output_paths(settings)
    original_lines, output_lines, rows, changed_ids, sanitize_errors = sanitize_rows(input_jsonl)
    errors = sanitize_errors + validate_v2(
        input_jsonl=input_jsonl,
        output_lines=output_lines,
        original_lines=original_lines,
        rows=rows,
        changed_ids=changed_ids,
    )
    summary = {
        "input_jsonl": str(input_jsonl),
        "output_jsonl": str(output_jsonl),
        "output_txt": str(output_txt),
        "line_count": len(output_lines),
        "changed_lines": sum(1 for old, new in zip(original_lines, output_lines) if old != new),
        "changed_ids": sorted(changed_ids),
        "candidate_count": sum(1 for row in rows if row.get("decision") == DECISION),
        "errors": errors,
    }
    if errors:
        output_txt.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raise RuntimeError("; ".join(errors))

    output_jsonl.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    output_txt.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_jsonl, output_txt, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-jsonl", required=True, type=Path)
    args = parser.parse_args()
    try:
        output_jsonl, output_txt, summary = run(input_jsonl=args.input_jsonl)
    except Exception as exc:
        print(f"[dynamic_acclaimed_knight_requirement_repair_v2_sanitize] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print("[dynamic_acclaimed_knight_requirement_repair_v2_sanitize] Completed")
    print(f"output_jsonl={output_jsonl}")
    print(f"output_txt={output_txt}")
    print(f"line_count={summary['line_count']}")
    print(f"changed_lines={summary['changed_lines']}")
    print(f"changed_ids={','.join(str(item) for item in summary['changed_ids'])}")
    print(f"candidate_count={summary['candidate_count']}")


if __name__ == "__main__":
    main()
