from __future__ import annotations

import collections
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path


INPUT_JSONL = Path(
    "reports/20260617_184054_single_combat_signature_weapon_composer_batch2_0041_decisions_reviewed_chat.jsonl"
)
DB_PATH = Path("memory/translation_engine.sqlite")
OUT_STEM = "single_combat_signature_weapon_composer_batch2_0041_decisions_reviewed_chat_v3"
TARGET_SEGMENT_ID = 246219

ALLOWED_DECISIONS = {
    "corrected",
    "already_good",
    "needs_context",
    "needs_token_delta_review",
    "blocked",
}
BAD_ENCODING_MARKERS = ("ï¿½", "Ãƒ", "Ã‚", "Ã¯Â¿Â½")
QUESTION_MARK_INSIDE_WORD_RE = re.compile(r"(?<=[A-Za-zÀ-ÿ])\?+(?=[A-Za-zÀ-ÿ])")
TOKEN_RE = re.compile(r"\[[^\]]+\]|#[A-Za-z0-9_]+|#!")

CORRECTED_TEXT = (
    "[sc_loser.GetFirstNameNoTooltip] luta melhor em terreno aberto e sabe disso; "
    "ceder terreno beneficia [sc_loser.GetHerHim]. Isso torna a luta frustrante "
    "até que eu veja por perto uma grande rocha de aparência afiada, bem #EMP na altura#! "
    "da canela.\\n\\nCom paciência, conduzo meu inimig[sc_loser.Custom('ES_OA')] "
    "em direção ao pequeno obstáculo, contando com seu reposicionamento arrogante."
)
REVIEW_NOTES = "corrigida ordem da frase e literalidade de justo; tokens #EMP/#! preservados"


def token_multiset(text: str) -> collections.Counter[str]:
    return collections.Counter(TOKEN_RE.findall(text or ""))


def load_rows() -> list[dict]:
    rows = []
    with INPUT_JSONL.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_output_texts(segment_ids: list[int]) -> dict[int, str]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in segment_ids)
        query = f"SELECT segment_id, portuguese_text FROM output_segments WHERE segment_id IN ({placeholders})"
        return {
            int(row["segment_id"]): row["portuguese_text"]
            for row in con.execute(query, segment_ids)
        }
    finally:
        con.close()


def apply_fix(rows: list[dict]) -> list[dict]:
    found = False
    fixed_rows = []
    for row in rows:
        row = dict(row)
        if row.get("segment_id") == TARGET_SEGMENT_ID:
            found = True
            row["corrected_text"] = CORRECTED_TEXT
            row["decision"] = "corrected"
            row["token_delta_review_required"] = False
            row["requires_apply_later"] = True
            row["tokens_preserved"] = True
            row["review_notes"] = REVIEW_NOTES
        fixed_rows.append(row)
    if not found:
        raise RuntimeError(f"target segment not found: {TARGET_SEGMENT_ID}")
    return fixed_rows


def validate(rows: list[dict]) -> list[str]:
    errors: list[str] = []
    if len(rows) != 31:
        errors.append(f"expected 31 rows, found {len(rows)}")

    counts = collections.Counter(row.get("decision") for row in rows)
    expected_counts = {
        "corrected": 23,
        "already_good": 3,
        "needs_context": 3,
        "needs_token_delta_review": 2,
    }
    for decision, expected in expected_counts.items():
        if counts.get(decision, 0) != expected:
            errors.append(f"expected {expected} {decision}, found {counts.get(decision, 0)}")

    unknown = sorted(set(counts) - ALLOWED_DECISIONS)
    if unknown:
        errors.append(f"unknown decisions: {unknown}")

    serialized = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    for marker in BAD_ENCODING_MARKERS:
        if marker in serialized:
            errors.append(f"bad encoding marker found: {marker}")
    if QUESTION_MARK_INSIDE_WORD_RE.search(serialized):
        errors.append("question mark found inside word")

    corrected_ids = [int(row["segment_id"]) for row in rows if row.get("decision") == "corrected"]
    output_texts = load_output_texts(corrected_ids)
    for row in rows:
        if row.get("decision") != "corrected":
            continue
        segment_id = int(row["segment_id"])
        corrected_text = row.get("corrected_text") or ""
        if not corrected_text:
            errors.append(f"empty corrected_text: {segment_id}")
        if row.get("token_delta_review_required") is True:
            errors.append(f"corrected has token_delta_review_required=true: {segment_id}")
        output_text = output_texts.get(segment_id, "")
        if token_multiset(corrected_text) != token_multiset(output_text):
            errors.append(
                f"token mismatch: {segment_id} corrected={dict(token_multiset(corrected_text))} "
                f"output={dict(token_multiset(output_text))}"
            )

    target = next((row for row in rows if row.get("segment_id") == TARGET_SEGMENT_ID), None)
    if not target:
        errors.append("target row missing after fix")
    else:
        target_text = target.get("corrected_text") or ""
        if "#EMP" not in target_text or "#!" not in target_text:
            errors.append("target row does not preserve #EMP and #!")

    return errors


def write_outputs(rows: list[dict], errors: list[str]) -> tuple[Path, Path]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path = Path("reports") / f"{timestamp}_{OUT_STEM}.jsonl"
    txt_path = Path("reports") / f"{timestamp}_{OUT_STEM}.txt"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    counts = collections.Counter(row.get("decision") for row in rows)
    target = next(row for row in rows if row.get("segment_id") == TARGET_SEGMENT_ID)
    lines = [
        "single_combat.0041 batch2 v3 tokenfix review",
        f"input={INPUT_JSONL}",
        f"jsonl={jsonl_path}",
        f"rows={len(rows)}",
        f"corrected={counts.get('corrected', 0)}",
        f"already_good={counts.get('already_good', 0)}",
        f"needs_context={counts.get('needs_context', 0)}",
        f"needs_token_delta_review={counts.get('needs_token_delta_review', 0)}",
        f"blocked={counts.get('blocked', 0)}",
        f"target_segment={TARGET_SEGMENT_ID}",
        f"target_source_key={target.get('source_key')}",
        f"target_review_notes={target.get('review_notes')}",
        f"validation_errors={len(errors)}",
    ]
    if errors:
        lines.extend(f"- {error}" for error in errors)
    else:
        lines.append("validation=ok")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path


def main() -> int:
    rows = apply_fix(load_rows())
    errors = validate(rows)
    jsonl_path, txt_path = write_outputs(rows, errors)
    print(f"jsonl={jsonl_path}")
    print(f"txt={txt_path}")
    print(f"rows={len(rows)}")
    print(f"errors={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
