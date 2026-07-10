from __future__ import annotations

import difflib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_pending_apply_run515_token_safe_diff_preview_v1"
INPUT_JSONL = Path(
    "reports/20260630_164232_747952_domain_policy_vote_candidate_pending_apply_confirmed_run515_diagnostic_token_safe_review_later.jsonl"
)
SEGMENT_STATE_RUN_ID = 515
EXPECTED_COUNT = 44


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def one_line_diff(before: str, after: str) -> str:
    return "\n".join(
        difflib.ndiff(
            before.splitlines() or [before],
            after.splitlines() or [after],
        )
    )


def change_family(output_text: str, confirmed_text: str) -> str:
    lower_pair = f"{output_text}\n{confirmed_text}".lower()
    if "pode se tornar" in confirmed_text and (
        "pode ser" in output_text or "pode tornar-se" in output_text or "pode ser reconhecido" in output_text
    ):
        return "accolade_unlock_can_become_formula"
    if "deixou de ser" in confirmed_text or "não é mais" in confirmed_text:
        return "status_or_state_change_phrase"
    if "região" in confirmed_text and "terra" in output_text:
        return "domain_word_choice_land_region"
    if "conex" in lower_pair or "contatos" in lower_pair:
        return "fluency_word_choice"
    if "\\n" in output_text or "\n" in confirmed_text:
        return "multiline_text_replacement"
    return "text_replacement_same_token"


def risk_note(row: dict[str, Any]) -> str:
    family = row["change_family"]
    if family == "accolade_unlock_can_become_formula":
        return "formulaica_recorrente_mas_multiline; requer revisao humana antes de apply"
    return "multiline_confirmacao_humana; requer diff preview aprovado antes de apply"


def representative_examples(rows: list[dict[str, Any]], limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = row["change_family"]
        if len(buckets[key]) >= limit:
            continue
        buckets[key].append(
            {
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "output_text": row["output_text"],
                "confirmed_text": row["confirmed_text"],
                "diff_preview": row["diff_preview"],
            }
        )
    return dict(buckets)


def main() -> None:
    input_rows = read_jsonl(INPUT_JSONL)
    if len(input_rows) != EXPECTED_COUNT:
        raise SystemExit(f"input count guard failed: {len(input_rows)}")

    records: list[dict[str, Any]] = []
    for row in input_rows:
        if int(row.get("segment_state_run_id") or 0) != SEGMENT_STATE_RUN_ID:
            raise SystemExit("segment_state_run_id guard failed")
        output_text = str(row.get("output_text") or "")
        confirmed_text = str(row.get("confirmed_text") or "")
        if not bool(row.get("token_integrity_ok")):
            raise SystemExit(f"token integrity guard failed for {row.get('segment_id')}")
        if int(row.get("needs_output_apply") or 0) != 1:
            raise SystemExit(f"needs_output_apply guard failed for {row.get('segment_id')}")
        if row.get("final_state") != "pending_apply_confirmed":
            raise SystemExit(f"final_state guard failed for {row.get('segment_id')}")
        family = change_family(output_text, confirmed_text)
        record = {
            "source": SOURCE,
            "segment_state_run_id": SEGMENT_STATE_RUN_ID,
            "segment_id": int(row["segment_id"]),
            "relative_path": row.get("relative_path"),
            "source_key": row.get("source_key"),
            "source_line_number": row.get("source_line_number"),
            "output_line_number": row.get("output_line_number"),
            "diagnostic_bucket": row.get("diagnostic_bucket"),
            "structural_surface": row.get("structural_surface"),
            "token_integrity_ok": bool(row.get("token_integrity_ok")),
            "needs_output_apply": int(row.get("needs_output_apply") or 0),
            "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
            "confirmation_level": row.get("confirmation_level"),
            "confirmation_source": row.get("confirmation_source"),
            "confirmation_label": row.get("confirmation_label"),
            "locked": int(row.get("locked") or 0),
            "change_family": family,
            "risk_note": "",
            "human_review_status": "pending_human_approval",
            "apply_allowed_now": False,
            "requires_diff_approval": True,
            "requires_snapshot_before_apply": True,
            "requires_post_validation_after_apply": True,
            "output_text": output_text,
            "confirmed_text": confirmed_text,
            "diff_preview": one_line_diff(output_text, confirmed_text),
        }
        record["risk_note"] = risk_note(record)
        records.append(record)

    family_counts = Counter(row["change_family"] for row in records)
    path_counts = Counter(str(row["relative_path"] or "").split("/", 1)[0] for row in records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_token_safe_diff_preview",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "input_jsonl": str(INPUT_JSONL),
        "candidate_count": 0,
        "diff_preview_count": len(records),
        "token_integrity_ok_count": sum(1 for row in records if row["token_integrity_ok"]),
        "structure_integrity_ok_count": 0,
        "requires_apply_later_count": len(records),
        "requires_human_approval_count": len(records),
        "requires_lifecycle_later_count": 0,
        "change_family_counts": dict(family_counts),
        "path_group_counts_top": [{"key": key, "count": count} for key, count in path_counts.most_common(20)],
        "representative_examples": representative_examples(records),
        "all_segment_ids": [int(row["segment_id"]) for row in records],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Review the 44 diff previews with the user. If approved, create a separate protected apply dry-run "
            "with snapshots and post-validation; keep token-changing holds out of that apply."
        ),
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_pending_apply_run515_token_safe_diff_preview"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, records)
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "domain_policy_vote_candidate pending apply run515 token-safe diff preview",
        "",
        f"segment_state_run_id: {SEGMENT_STATE_RUN_ID}",
        f"diff_preview_count: {len(records)}",
        f"token_integrity_ok_count: {summary['token_integrity_ok_count']}",
        f"requires_human_approval_count: {summary['requires_human_approval_count']}",
        "",
        "change_family_counts:",
        *[f"- {count} | {key}" for key, count in family_counts.most_common()],
        "",
        "path_group_counts_top:",
        *[f"- {item['count']} | {item['key']}" for item in summary["path_group_counts_top"]],
        "",
        "guards:",
        "- candidate_generation: not_run",
        "- apply: not_run",
        "- lifecycle: not_run",
        "- segment_state: not_run",
        "- reindex: not_run",
        "- full_production: not_run",
        "",
        "diff previews:",
    ]
    for row in records:
        lines.extend(
            [
                "",
                f"segment_id: {row['segment_id']}",
                f"path: {row['relative_path']}",
                f"key: {row['source_key']}",
                f"family: {row['change_family']}",
                "output:",
                row["output_text"],
                "confirmed:",
                row["confirmed_text"],
                "diff:",
                row["diff_preview"],
            ]
        )
    lines.extend(["", f"recommendation: {summary['single_operational_recommendation']}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
