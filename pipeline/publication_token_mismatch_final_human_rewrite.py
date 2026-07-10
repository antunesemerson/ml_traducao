from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import structural_tokens


RULE_VERSION = "publication_token_mismatch_final_human_rewrite_v1"
CONFIRMATION_SOURCE = "codex_publication_token_mismatch_human_rewrite"
CONFIRMATION_LABEL = "publication_token_mismatch_final_human_rewrite"

CORRECTIONS: dict[int, str] = {
    3459: (
        "Enquanto exploro, ouço sem querer uma conversa entre "
        "[Select_CString(And(local_character.IsFemale, local_character_2.IsFemale),'duas moradoras instruídas','dois moradores instruídos')]. "
        "\"Nunca foi tão fácil distribuir as esmolas desde que você organizou o lugar. Certamente "
        "[local_character.GetFaith.HighGodName|U] te abençoará, [local_character_2.GetFirstName]\", diz "
        "[local_character.GetFirstName].\n\n"
        "\"Vós me honrais, [local_character.GetFirstName], mas vós também sois uma pessoa devota, "
        "muito querida e de coração [local_character.Custom('ComplimentAdjective')] aos olhos da divindade. "
        "Eu apenas cumpro meu trabalho como responsável pelo lugar especial de "
        "[location.GetHolding.GetSpecialBuildingType.GetNameNoTooltip]\", continua [local_character_2.GetFirstName]."
    ),
    137763: (
        "[ROOT.Char.Custom2('RelationToMe', SCOPE.sC('3100_courtier_liege'))|U] "
        "[3100_courtier_liege.GetTitledFirstName] me honrou com uma visita e trouxe um de seus "
        "[3100_courtier_liege.Custom('GetCourtierPlural')] consigo. "
        "[3100_courtier_liege.GetFirstNameNoTooltip] se aproxima de mim com entusiasmo:\n\n"
        "\"Prazer em ver você, [ROOT.Char.GetTitledFirstNameNoTooltip]! Alegra-me ver que prospera aqui em "
        "[ROOT.Char.GetCapitalLocation.GetName]. Ocorreu-me que [3100_target_courtier.GetFirstName], "
        "aqui presente, poderia ser de utilidade em sua [ROOT.Char.Custom('GetCourt')]. "
    ),
}


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def short(value: str | None, limit: int = 220) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def has_spanish_helper(value: str) -> bool:
    helpers = ("Custom('ES_", 'Custom("ES_')
    return any(helper in value for helper in helpers)


def fetch_row(conn, segment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            sc.id AS confirmation_id,
            sc.segment_id,
            sc.confirmation_level,
            sc.confirmation_source,
            sc.confirmation_label,
            sc.locked,
            sc.confirmed_text,
            ss.relative_path,
            ss.source_key,
            ss.spanish_text AS source_text,
            ss.old_text,
            os.portuguese_text AS output_text
        FROM segment_confirmations sc
        JOIN source_segments ss ON ss.id = sc.segment_id
        LEFT JOIN output_segments os ON os.segment_id = sc.segment_id
        WHERE sc.segment_id = ?
        """,
        (segment_id,),
    ).fetchone()
    return dict(row) if row else None


def classify(row: dict[str, Any], corrected_text: str) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if not corrected_text.strip():
        return "blocked", ["empty_corrected_text"]
    if has_spanish_helper(corrected_text):
        return "blocked", ["corrected_text_still_has_spanish_helper"]
    if corrected_text == (row.get("confirmed_text") or ""):
        return "already_matches", ["confirmed_text_already_matches"]
    if not has_spanish_helper(row.get("confirmed_text") or ""):
        reasons.append("current_confirmed_text_no_spanish_helper")
    old_tokens = structural_tokens(row.get("confirmed_text") or "")
    new_tokens = structural_tokens(corrected_text)
    removed_tokens = [token for token in old_tokens if token not in new_tokens]
    added_tokens = [token for token in new_tokens if token not in old_tokens]
    if removed_tokens:
        reasons.append("intentional_token_rewrite_removes_spanish_helpers_or_fragments")
    if added_tokens:
        reasons.append("intentional_token_rewrite_adds_safe_runtime_tokens_or_preserves_context")
    return "ready", reasons


def write_reports(settings: dict, records: list[dict[str, Any]], *, apply: bool, applied: int) -> tuple[Path, Path, Path]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = f"{timestamp}_publication_token_mismatch_final_human_rewrite"
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = reports_dir / f"{stem}.jsonl"
    summary_path = reports_dir / f"{stem}_summary.json"
    md_path = reports_dir / f"{stem}.md"

    jsonl_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    counts = Counter(record["status"] for record in records)
    summary = {
        "rule_version": RULE_VERSION,
        "apply": apply,
        "record_count": len(records),
        "applied": applied,
        "status_counts": dict(counts),
        "segments": [record["segment_id"] for record in records],
        "decisions_jsonl": None,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Publication Token Mismatch Final Human Rewrite",
        "",
        f"- Rule version: `{RULE_VERSION}`",
        f"- Apply: `{apply}`",
        f"- Records: `{len(records)}`",
        f"- Applied: `{applied}`",
        "",
    ]
    for record in records:
        lines.extend(
            [
                f"## Segment {record['segment_id']} - {record['status']}",
                "",
                f"- File/key: `{record['relative_path']} :: {record['source_key']}`",
                f"- Reasons: `{', '.join(record['reasons'])}`",
                f"- Old confirmed hash: `{record['old_confirmed_hash']}`",
                f"- New confirmed hash: `{record['new_confirmed_hash']}`",
                "",
                "Current confirmation:",
                "",
                f"> {short(record['old_confirmed_text'], 500)}",
                "",
                "Corrected confirmation:",
                "",
                f"> {short(record['corrected_text'], 500)}",
                "",
            ]
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, jsonl_path, summary_path


def write_decisions(settings: dict, records: list[dict[str, Any]], *, policy_run_id: int) -> Path:
    conn = db.connect(settings)
    conn.row_factory = db.sqlite3.Row
    decisions: list[dict[str, Any]] = []
    try:
        for record in records:
            if record["status"] != "ready":
                continue
            row = conn.execute(
                """
                SELECT id
                FROM segment_token_policy_items
                WHERE run_id = ?
                  AND segment_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (policy_run_id, record["segment_id"]),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"No policy item found for segment {record['segment_id']} in policy run {policy_run_id}.")
            decisions.append(
                {
                    "policy_item_id": int(row["id"]),
                    "segment_id": record["segment_id"],
                    "decision": "accept_policy_candidate",
                    "corrected_text": record["corrected_text"],
                    "notes": "Final human rewrite removes Spanish gender helpers while preserving intended runtime localization.",
                    "reviewer": CONFIRMATION_SOURCE,
                }
            )
    finally:
        conn.close()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = db.project_path(settings["reports_dir"]) / f"{timestamp}_publication_token_mismatch_final_human_rewrite_decisions.jsonl"
    path.write_text(
        "\n".join(json.dumps(decision, ensure_ascii=False) for decision in decisions) + "\n",
        encoding="utf-8",
    )
    return path


def main(*, apply: bool = False, policy_run_id: int = 62) -> None:
    settings = db.load_settings()
    records: list[dict[str, Any]] = []
    applied = 0
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        now = db.utc_now()
        for segment_id, corrected_text in CORRECTIONS.items():
            row = fetch_row(conn, segment_id)
            if row is None:
                records.append({"segment_id": segment_id, "status": "missing_confirmation", "reasons": ["missing_confirmation"]})
                continue
            status, reasons = classify(row, corrected_text)
            record = {
                "segment_id": segment_id,
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "status": status,
                "reasons": reasons,
                "old_confirmation_level": row["confirmation_level"],
                "old_confirmation_source": row["confirmation_source"],
                "old_confirmation_label": row["confirmation_label"],
                "old_locked": row["locked"],
                "old_confirmed_hash": sha256_text(row.get("confirmed_text")),
                "new_confirmed_hash": sha256_text(corrected_text),
                "output_hash": sha256_text(row.get("output_text")),
                "old_confirmed_text": row.get("confirmed_text"),
                "corrected_text": corrected_text,
                "old_structural_tokens": structural_tokens(row.get("confirmed_text") or ""),
                "new_structural_tokens": structural_tokens(corrected_text),
            }
            records.append(record)
            if apply and status == "ready":
                conn.execute(
                    """
                    UPDATE segment_confirmations
                    SET confirmed_text = ?,
                        confirmation_level = 'human_confirmed',
                        confirmation_source = ?,
                        confirmation_label = ?,
                        locked = 1,
                        reviewer = ?,
                        confidence_score = 1.0,
                        updated_at = ?,
                        confirmed_at = COALESCE(confirmed_at, ?)
                    WHERE id = ?
                    """,
                    (
                        corrected_text,
                        CONFIRMATION_SOURCE,
                        CONFIRMATION_LABEL,
                        CONFIRMATION_SOURCE,
                        now,
                        now,
                        row["confirmation_id"],
                    ),
                )
                applied += 1
        if apply:
            conn.commit()

    md_path, jsonl_path, summary_path = write_reports(settings, records, apply=apply, applied=applied)
    decisions_path = write_decisions(settings, records, policy_run_id=policy_run_id)
    print("[publication_token_mismatch_final_human_rewrite] Done")
    print(f"[publication_token_mismatch_final_human_rewrite] Apply: {apply}")
    print(f"[publication_token_mismatch_final_human_rewrite] Records: {len(records)}")
    print(f"[publication_token_mismatch_final_human_rewrite] Applied: {applied}")
    print(f"[publication_token_mismatch_final_human_rewrite] Report: {md_path}")
    print(f"[publication_token_mismatch_final_human_rewrite] JSONL: {jsonl_path}")
    print(f"[publication_token_mismatch_final_human_rewrite] Summary: {summary_path}")
    print(f"[publication_token_mismatch_final_human_rewrite] Decisions: {decisions_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply final human confirmation rewrites for publication token mismatches.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--policy-run-id", type=int, default=62)
    args = parser.parse_args()
    main(apply=args.apply, policy_run_id=args.policy_run_id)
