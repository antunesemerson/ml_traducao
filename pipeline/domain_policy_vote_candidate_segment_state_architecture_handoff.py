from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_segment_state_architecture_handoff_v1"
SUMMARY_PATH = Path(
    "reports/20260630_143758_178640_domain_policy_vote_candidate_confirmed_output_state_divergence_audit_summary.json"
)
JSONL_PATH = Path(
    "reports/20260630_143758_178640_domain_policy_vote_candidate_confirmed_output_state_divergence_audit.jsonl"
)
PROMPT_PATH = Path("prompts/chat_architecture_segment_state_confirmed_output_guard_review_prompt.md")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main() -> None:
    audit_summary = read_json(SUMMARY_PATH)
    audit_rows = read_jsonl(JSONL_PATH)
    focus_ids = set(audit_summary["focus_segment_ids"])
    focus_rows = [row for row in audit_rows if int(row["segment_id"]) in focus_ids]
    if len(focus_rows) != int(audit_summary["focus_count"]):
        raise SystemExit("focus row count guard failed")
    if int(audit_summary["candidate_generation_count"]) != 0:
        raise SystemExit("candidate_generation guard failed")
    if int(audit_summary["apply_count"]) != 0:
        raise SystemExit("apply guard failed")
    if int(audit_summary["lifecycle_count"]) != 0:
        raise SystemExit("lifecycle guard failed")
    if int(audit_summary["segment_state_count"]) != 0:
        raise SystemExit("segment_state guard failed")
    if int(audit_summary["reindex_count"]) != 0:
        raise SystemExit("reindex guard failed")
    if int(audit_summary["production_full_count"]) != 0:
        raise SystemExit("production guard failed")

    handoff = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_architecture_handoff",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "lane_hold": {
            "agent_key": "domain_policy_vote_candidate",
            "discovery_hold": True,
            "apply_hold": True,
            "lifecycle_hold": True,
            "hold_reason": "segment_state_snapshot confirmed/output guards require architecture review",
        },
        "input_files": {
            "audit_summary": str(SUMMARY_PATH),
            "audit_jsonl": str(JSONL_PATH),
            "architecture_prompt": str(PROMPT_PATH),
        },
        "segment_state_run_id": audit_summary["segment_state_run_id"],
        "total_divergence_count": audit_summary["total_divergence_count"],
        "state_confirmed_matches_output_false_positive_count": audit_summary[
            "state_confirmed_matches_output_false_positive_count"
        ],
        "needs_output_apply_false_negative_count": audit_summary["needs_output_apply_false_negative_count"],
        "diff_kind_counts": audit_summary["diff_kind_counts"],
        "explicit_buckets": {
            "text_divergence_same_token_signature": 377,
            "protected_token_signature_mismatch": 7,
            "holy_sites_plural_to_singular_only": 6,
        },
        "holy_site_focus_segments": [
            {
                "segment_id": int(row["segment_id"]),
                "source_key": row["source_key"],
                "output_text": row["output_text"],
                "confirmed_text": row["confirmed_text"],
                "diff_kind": row["diff_kind"],
                "token_integrity_ok": row["token_integrity_ok"],
            }
            for row in focus_rows
        ],
        "architecture_request": {
            "fix_confirmed_matches_output_real_text_equality": True,
            "fix_needs_output_apply_real_text_divergence": True,
            "block_lifecycle_when_confirmed_differs_from_output": True,
            "separate_token_changing_corrections": True,
        },
        "acceptance_criteria": [
            "confirmed_matches_output is 0 whenever confirmed_text != output_text",
            "needs_output_apply is 1 whenever confirmed_text != output_text unless explicit hold prevents closure",
            "token-changing corrections remain hold/needs_token_policy",
            "no source/output writes from architecture guard change",
            "after architecture adjustment, training chat runs only segment-state and delta against 512",
        ],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_segment_state_architecture_handoff"
    txt_path = base.with_suffix(".txt")
    summary_path = Path(str(base) + "_summary.json")
    handoff["output_files"] = {"txt": str(txt_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(handoff, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "domain_policy_vote_candidate segment-state architecture handoff",
        "",
        "hold:",
        "- discovery: true",
        "- apply: true",
        "- lifecycle: true",
        "",
        f"segment_state_run_id: {handoff['segment_state_run_id']}",
        f"total_divergence_count: {handoff['total_divergence_count']}",
        f"state_confirmed_matches_output_false_positive_count: {handoff['state_confirmed_matches_output_false_positive_count']}",
        f"needs_output_apply_false_negative_count: {handoff['needs_output_apply_false_negative_count']}",
        "",
        "diff_kind_counts:",
    ]
    for key, count in handoff["diff_kind_counts"].items():
        lines.append(f"- {count} | {key}")
    lines.extend(
        [
            "",
            "architecture request:",
            "- confirmed_matches_output must use real text equality confirmed_text == output_text",
            "- needs_output_apply must be true when confirmed_text != output_text",
            "- lifecycle closure must not happen while confirmed_text != output_text",
            "- token-changing corrections remain hold/needs_token_policy",
            "",
            f"prompt: {PROMPT_PATH}",
            "",
            "guards:",
            "- candidate_generation: not_run",
            "- apply: not_run",
            "- lifecycle: not_run",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(handoff, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
