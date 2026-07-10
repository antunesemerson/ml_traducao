from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import structural_tokens


BAD_ENCODING_RE = re.compile(r"Ãƒ|Ã‚|�")
QUESTION_INSIDE_WORD_RE = re.compile(r"\w\?\w", re.UNICODE)
DYNAMIC_RE = re.compile(r"Select_CString|Custom\(|(?:^|[.\[])\s*Get[A-Za-z_]*\b|Concept\(|ScriptValue\(", re.IGNORECASE)
DOMAIN_RE = re.compile(r"culture|religion|title|nicknames?|traits?|laws?|accolades?|legends?", re.IGNORECASE)

SAFE_REPAIRS = {
    27635: ("#D     Força atribuída: $NUM$#!", "spanish_residual", "mechanical Spanish UI label translation"),
    27645: ("#D     Força atribuída real: $NUM$#!", "spanish_residual", "mechanical Spanish UI label translation"),
    35879: ("Requintada", "spanish_residual", "false-cognate adjective repaired mechanically"),
    42215: ("Brasão Requintado", "spanish_residual", "false-cognate adjective repaired mechanically"),
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def classify(row: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    current = str(row.get("current_text") or "")
    relative_path = str(row.get("relative_path") or "")
    families = set(row.get("open_issue_families") or [])
    kinds = set(row.get("open_issue_kinds") or [])
    issue_text = " ".join(sorted(families | kinds)).lower()

    if state is None or state["state_group"] != "pending":
        decision = "blocked_uncertain"
        repair_kind = ""
        corrected = ""
        notes = "segment is not pending in requested snapshot"
    elif int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        decision = "blocked_uncertain"
        repair_kind = ""
        corrected = ""
        notes = "state guards are not clean for future repair review"
    elif segment_id in SAFE_REPAIRS:
        corrected, repair_kind, notes = SAFE_REPAIRS[segment_id]
        decision = "safe_spanish_residual_repair"
        if structural_tokens(current) != structural_tokens(corrected):
            decision = "blocked_uncertain"
            repair_kind = ""
            corrected = ""
            notes = "safe repair rejected because CK3 tokens would not be preserved"
    elif DYNAMIC_RE.search(current):
        decision = "needs_dynamic_expression_agent"
        repair_kind = ""
        corrected = ""
        notes = "dynamic CK3 expression needs a dedicated expression/context agent"
    elif any(marker in current for marker in ("GetFaith", "GetFirstName", "GetSheHe", "GetHerHis")):
        decision = "needs_dynamic_expression_agent"
        repair_kind = ""
        corrected = ""
        notes = "dynamic named/gendered expression blocks mechanical residual repair"
    elif DOMAIN_RE.search(relative_path) or "HighGodName" in current or "Valhalla" in current or "Orange" in current:
        decision = "needs_domain_context"
        repair_kind = ""
        corrected = ""
        notes = "domain-sensitive or named entity phrase needs context"
    elif "high_issue_present" in issue_text or current.startswith(("\"", "'")):
        decision = "needs_context_composer"
        repair_kind = ""
        corrected = ""
        notes = "quoted/event dialogue requires narrative context before repair"
    elif " te " in f" {current.lower()} " or current.lower().startswith(("te ", "me ")):
        decision = "needs_context_composer"
        repair_kind = ""
        corrected = ""
        notes = "pronoun/register issue needs speaker/addressee context, not mechanical residual repair"
    elif any(spanish in current.lower() for spanish in ("fuerza", "exquisit", " se le ")):
        decision = "needs_semantic_review"
        repair_kind = ""
        corrected = ""
        notes = "Spanish-looking residue not covered by safe mechanical map"
    else:
        decision = "needs_semantic_review"
        repair_kind = ""
        corrected = ""
        notes = "residual flag appears semantic or false positive and needs review"

    requires_apply = bool(corrected)
    return {
        "segment_id": segment_id,
        "key": row["key"],
        "relative_path": row["relative_path"],
        "current_text": current,
        "residual_decision": decision,
        "repair_kind": repair_kind,
        "corrected_text": corrected,
        "requires_apply_later": requires_apply,
        "requires_lifecycle_later": False,
        "open_issue_families": row.get("open_issue_families", []),
        "open_issue_kinds": row.get("open_issue_kinds", []),
        "notes": notes,
    }


def validate_review(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        corrected = row["corrected_text"]
        if row["requires_apply_later"] and not corrected:
            raise RuntimeError(f"Missing corrected_text for apply candidate {row['segment_id']}")
        if not corrected:
            continue
        if BAD_ENCODING_RE.search(corrected):
            raise RuntimeError(f"Bad encoding marker in corrected_text for {row['segment_id']}")
        if QUESTION_INSIDE_WORD_RE.search(corrected):
            raise RuntimeError(f"Question mark inside word in corrected_text for {row['segment_id']}")
        if structural_tokens(row["current_text"]) != structural_tokens(corrected):
            raise RuntimeError(f"CK3 tokens not preserved for {row['segment_id']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--companion-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    args = parser.parse_args()

    companion_rows = read_jsonl(db.project_path(args.companion_jsonl))
    residual_rows = [row for row in companion_rows if row.get("companion_decision") == "needs_residual_repair"]
    segment_ids = [int(row["segment_id"]) for row in residual_rows]
    placeholders = ",".join("?" for _ in segment_ids)
    conn = db.connect()
    state_by_segment = {
        int(row["segment_id"]): dict(row)
        for row in conn.execute(
            f"""
            SELECT segment_id, state_group, needs_output_apply, confirmed_matches_output, needs_reopen, locked
            FROM segment_state_items
            WHERE run_id = ?
              AND segment_id IN ({placeholders})
            """,
            (args.segment_state_run_id, *segment_ids),
        ).fetchall()
    }
    conn.close()

    reviewed = [classify(row, state_by_segment.get(int(row["segment_id"]))) for row in residual_rows]
    validate_review(reviewed)

    settings = db.load_settings()
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    jsonl_path = reports_dir / f"{stamp}_short_label_style_batch3_residual_repair_split.jsonl"
    txt_path = reports_dir / f"{stamp}_short_label_style_batch3_residual_repair_split.txt"
    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in reviewed) + "\n",
        encoding="utf-8",
    )

    decision_counts = Counter(row["residual_decision"] for row in reviewed)
    apply_candidates = [row for row in reviewed if row["requires_apply_later"]]
    lines = [
        "Short-label style batch3 residual repair split",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"segment_state_run_id: {args.segment_state_run_id}",
        f"ledger_run_id: {args.ledger_run_id}",
        f"companion_jsonl: {args.companion_jsonl}",
        f"reviewed: {len(reviewed)}",
        f"future_apply_candidates: {len(apply_candidates)}",
        "",
        "Counts by residual_decision:",
    ]
    for decision, count in decision_counts.most_common():
        lines.append(f"- {decision}: {count}")
    lines.extend(["", "Safe examples:"])
    if apply_candidates:
        for row in apply_candidates:
            lines.append(f"- {row['segment_id']} | {row['current_text']} -> {row['corrected_text']}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Recommendation:",
            "- Do not prepare protected apply yet from this split alone: safe_*_repair is below 5.",
            "- Prioritize dynamic expression/gender/custom-loc policy and context/semantic review for the remaining blockers.",
            "",
            "Safety confirmation: read-only split only; no production, no apply, no lifecycle, no segment-state, no confirmations, no reindex, no training/model promotion, no source/output edits.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"reviewed={len(reviewed)}")
    print(f"future_apply_candidates={len(apply_candidates)}")
    print(f"decision_counts={dict(decision_counts)}")
    print(f"jsonl={jsonl_path}")
    print(f"txt={txt_path}")


if __name__ == "__main__":
    main()
