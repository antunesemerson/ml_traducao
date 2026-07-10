from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|\.Custom\('ES_[A-Za-z0-9_]+'\)"
)

APPROVED: dict[int, tuple[str, str]] = {
    36200: (
        "[lifestyle_character.GetSheHe] se levanta todas as manhãs para treinar, "
        "brandindo sua espada de um lado para o outro.\\n\\n"
        "Até esta mesma noite, quando os outros estão ocupados apreciando a comida, "
        "[lifestyle_character.GetSheHe] saiu por conta própria para fazer exercícios que inspiram admiração. "
        "Não consigo evitar querer emular partes de seu estilo de vida.",
        "human approved complete gender-token repair; replace hardcoded opening pronoun and remove duplicated pronoun after token",
    ),
    281562: (
        "Este personagem se entregou à bebida e está sempre procurando chegar ao fundo da garrafa.",
        "human approved softened trait wording with masculine generic",
    ),
}

FALSE_POSITIVE_IDS = {281408, 281427, 281445, 281565, 281583, 281586}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def token_inventory(value: str) -> list[str]:
    return TOKEN_RE.findall(value)


def decide(row: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    current = str(row.get("current_output_text") or "")
    if segment_id in APPROVED:
        candidate, reason = APPROVED[segment_id]
        token_integrity_ok = token_inventory(current) == token_inventory(candidate)
        structure_integrity_ok = current.count("\\n") == candidate.count("\\n") and current.count("#") == candidate.count("#")
        decision = "human_approved_for_protected_apply" if token_integrity_ok and structure_integrity_ok else "blocked_integrity_guard"
        return {
            **row,
            "human_decision": decision,
            "human_reason": reason,
            "candidate_text": candidate,
            "safe_for_future_apply_batch": decision == "human_approved_for_protected_apply",
            "requires_apply_later": decision == "human_approved_for_protected_apply",
            "requires_lifecycle_later": False,
            "token_integrity_ok": token_integrity_ok,
            "structure_integrity_ok": structure_integrity_ok,
            "reviewed_by_human": True,
        }
    if segment_id in FALSE_POSITIVE_IDS:
        return {
            **row,
            "human_decision": "human_rejected_false_positive_no_change",
            "human_reason": "human review kept local traits style; no change",
            "candidate_text": "",
            "safe_for_future_apply_batch": False,
            "requires_apply_later": False,
            "requires_lifecycle_later": False,
            "token_integrity_ok": True,
            "structure_integrity_ok": True,
            "reviewed_by_human": True,
        }
    return {
        **row,
        "human_decision": "not_in_this_human_decision_batch",
        "human_reason": "not selected for approval in this review cycle",
        "safe_for_future_apply_batch": False,
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
        "reviewed_by_human": False,
    }


def output_paths() -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_gender_semantic_literal_residue_human_decisions"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir() / f"{base.name}_summary.json"


def write_outputs(records: list[dict[str, Any]], input_audit_jsonl: str) -> tuple[Path, Path, Path, dict[str, Any]]:
    txt_path, jsonl_path, summary_path = output_paths()
    decisions = Counter(row["human_decision"] for row in records)
    approved = [row for row in records if row.get("safe_for_future_apply_batch")]
    summary = {
        "schema_version": 1,
        "source": "gender_semantic_literal_residue_human_decisions_v1",
        "input_audit_jsonl": input_audit_jsonl,
        "reviewed_count": len(records),
        "decision_counts": dict(sorted(decisions.items())),
        "safe_for_future_apply_batch_count": len(approved),
        "accepted_candidate_ids": [int(row["segment_id"]) for row in approved],
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "lifecycle_reindex_recommended_now": False,
        "next_action": "next_cycle_dry_run_diff_preview_for_approved_only",
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Gender semantic literal-residue human decisions",
        f"reviewed_count={summary['reviewed_count']}",
        f"safe_for_future_apply_batch_count={summary['safe_for_future_apply_batch_count']}",
        f"accepted_candidate_ids={summary['accepted_candidate_ids']}",
        "",
        "Decision counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(decisions.items()))
    lines.extend(
        [
            "",
            "Safety: human decisions only; no apply, no lifecycle/reindex, no training, no source/output edits.",
            f"next_action={summary['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-jsonl", required=True)
    args = parser.parse_args()
    source_rows = read_jsonl(db.project_path(args.audit_jsonl))
    relevant = [
        row
        for row in source_rows
        if int(row["segment_id"]) in APPROVED or int(row["segment_id"]) in FALSE_POSITIVE_IDS
    ]
    records = [decide(row) for row in relevant]
    txt_path, jsonl_path, summary_path, summary = write_outputs(records, args.audit_jsonl)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"reviewed_count={summary['reviewed_count']}")
    print(f"safe_for_future_apply_batch_count={summary['safe_for_future_apply_batch_count']}")
    print(f"accepted_candidate_ids={summary['accepted_candidate_ids']}")
    print("decision_counts=" + json.dumps(summary["decision_counts"], ensure_ascii=False, sort_keys=True))
    print("next_action=" + summary["next_action"])


if __name__ == "__main__":
    main()
