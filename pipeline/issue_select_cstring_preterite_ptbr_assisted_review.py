from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_select_cstring_preterite_ptbr_assisted_review_v1"
DEFAULT_SAMPLE_GLOB = "*_issue_select_cstring_preterite_ptbr_evidence_queue_validation_sample.jsonl"
TARGET_AGENT = "select_cstring_local_player_preterite_verb_rewrite"

PTBR_VERB_MAP = {
    "accediste": ("edit_ptbr_neutral_literal", "Teve acesso", "context-reviewed CK3 power access phrase"),
    "aceptaste": ("edit_ptbr_neutral_literal", "aceitou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "aconsejaste": ("edit_ptbr_neutral_literal", "aconselhou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "admiraste": ("edit_ptbr_neutral_literal", "admirou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "afirmaste": ("edit_ptbr_neutral_literal", "afirmou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "ajustaste": ("edit_ptbr_neutral_literal", "ajustou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "alabaste": ("edit_ptbr_neutral_literal", "elogiou", "regular Spanish verb mapped to natural PT-BR past tense"),
    "arrebataste": ("edit_ptbr_neutral_literal", "arrebatou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "asustaste": ("edit_ptbr_neutral_literal", "assustou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "atendiste": ("edit_ptbr_neutral_literal", "atendeu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "ayudaste": ("edit_ptbr_neutral_literal", "ajudou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "apostaste": ("edit_ptbr_neutral_literal", "apostou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "apoyaste": ("edit_ptbr_neutral_literal", "apoiou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "aprobaste": ("edit_ptbr_neutral_literal", "aprovou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "aste": ("edit_ptbr_neutral_literal", "ou", "context-reviewed suffix repair for começou"),
    "caminaste": ("edit_ptbr_neutral_literal", "caminhou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "cargaste": ("edit_ptbr_neutral_literal", "carregou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "causaste": ("edit_ptbr_neutral_literal", "causou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "cazaste": ("edit_ptbr_neutral_literal", "caçou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "celebraste": ("edit_ptbr_neutral_literal", "celebrou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "colmaste": ("edit_ptbr_neutral_literal", "cobriu", "context-reviewed charity phrase: cobriu de grandes merces"),
    "compartiste": ("edit_ptbr_neutral_literal", "compartilhou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "compraste": ("edit_ptbr_neutral_literal", "comprou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "condujiste": ("edit_ptbr_neutral_literal", "conduziu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "conseguiste": ("edit_ptbr_neutral_literal", "conseguiu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "consolaste": ("edit_ptbr_neutral_literal", "consolou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "contaste": ("edit_ptbr_neutral_literal", "contou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "conquistaste": ("edit_ptbr_neutral_literal", "conquistou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "convertiste": ("edit_ptbr_neutral_literal", "converteu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "decidiste": ("edit_ptbr_neutral_literal", "decidiu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "debatiste": ("edit_ptbr_neutral_literal", "debateu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "demostraste": ("edit_ptbr_neutral_literal", "demonstrou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "descubriste": ("edit_ptbr_neutral_literal", "descobriu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "desdeñaste": ("edit_ptbr_neutral_literal", "desdenhou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "desterraste": ("edit_ptbr_neutral_literal", "exilou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "devolviste": ("edit_ptbr_neutral_literal", "devolveu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "defendiste": ("edit_ptbr_neutral_literal", "defendeu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "deseaste": ("edit_ptbr_neutral_literal", "desejou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "difundiste": ("edit_ptbr_neutral_literal", "difundiu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "dirigiste": ("edit_ptbr_neutral_literal", "dirigiu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "disfrutaste": ("edit_ptbr_neutral_literal", "desfrutou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "donaste": ("edit_ptbr_neutral_literal", "doou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "dudaste": ("edit_ptbr_neutral_literal", "duvidou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "embelesaste": ("edit_ptbr_neutral_literal", "encantou", "context-reviewed enchanted-by-dancing phrase"),
    "encontraste": ("edit_ptbr_neutral_literal", "encontrou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "enseñaste": ("edit_ptbr_neutral_literal", "ensinou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "entendiste": ("edit_ptbr_neutral_literal", "entendeu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "entablaste": ("edit_ptbr_neutral_literal", "travou", "context-reviewed discussion/debate phrase"),
    "escuchaste": ("edit_ptbr_neutral_literal", "escutou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "esperaste": ("edit_ptbr_neutral_literal", "esperou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "esculpiste": ("edit_ptbr_neutral_literal", "esculpiu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "exhibiste": ("edit_ptbr_neutral_literal", "exibiu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "exploraste": ("edit_ptbr_neutral_literal", "explorou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "explicaste": ("edit_ptbr_neutral_literal", "explicou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "expulsaste": ("edit_ptbr_neutral_literal", "expulsou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "financiaste": ("edit_ptbr_neutral_literal", "financiou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "hablaste": ("edit_ptbr_neutral_literal", "falou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "huiste": ("edit_ptbr_neutral_literal", "fugiu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "impartiste": ("edit_ptbr_neutral_literal", "transmitiu", "context-reviewed wisdom phrase"),
    "impresionaste": ("edit_ptbr_neutral_literal", "impressionou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "juraste": ("edit_ptbr_neutral_literal", "jurou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "levantaste": ("edit_ptbr_neutral_literal", "levantou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "liberaste": ("edit_ptbr_neutral_literal", "libertou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "llevaste": ("edit_ptbr_neutral_literal", "levou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "lloraste": ("edit_ptbr_neutral_literal", "chorou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "masacraste": ("edit_ptbr_neutral_literal", "massacrou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "mataste": ("edit_ptbr_neutral_literal", "matou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "moriste": ("edit_ptbr_neutral_literal", "morreu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "mostraste": ("edit_ptbr_neutral_literal", "mostrou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "observaste": ("edit_ptbr_neutral_literal", "observou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "obligaste": ("edit_ptbr_neutral_literal", "obrigou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "ofendiste": ("edit_ptbr_neutral_literal", "ofendeu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "ofreciste": ("edit_ptbr_neutral_literal", "ofereceu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "olvidaste": ("edit_ptbr_neutral_literal", "esqueceu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "organizaste": ("edit_ptbr_neutral_literal", "organizou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "osaste": ("edit_ptbr_neutral_literal", "ousou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "participaste": ("edit_ptbr_neutral_literal", "participou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "perdiste": ("edit_ptbr_neutral_literal", "perdeu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "predicaste": ("edit_ptbr_neutral_literal", "pregou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "presidiste": ("edit_ptbr_neutral_literal", "presidiu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "proclamaste": ("edit_ptbr_neutral_literal", "proclamou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "profanaste": ("edit_ptbr_neutral_literal", "profanou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "promoviste": ("edit_ptbr_neutral_literal", "promoveu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "pronunciaste": ("edit_ptbr_neutral_literal", "pronunciou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "quitaste": ("edit_ptbr_neutral_literal", "removeu", "context-reviewed removal phrase"),
    "realizaste": ("edit_ptbr_neutral_literal", "realizou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "recibiste": ("edit_ptbr_neutral_literal", "recebeu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "reclutaste": ("edit_ptbr_neutral_literal", "recrutou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "reencarcelaste": ("edit_ptbr_neutral_literal", "reencarcerou", "regular Spanish verb mapped to CK3-style prison context"),
    "reflexionaste": ("edit_ptbr_neutral_literal", "refletiu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "regateaste": ("edit_ptbr_neutral_literal", "pechinchou", "regular Spanish verb mapped to natural PT-BR past tense"),
    "reparaste": ("edit_ptbr_neutral_literal", "reparou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "resististe": ("edit_ptbr_neutral_literal", "resistiu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "respaldaste": ("edit_ptbr_neutral_literal", "apoiou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "reuniste": ("edit_ptbr_neutral_literal", "reuniu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "rezaste": ("edit_ptbr_neutral_literal", "rezou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "robaste": ("edit_ptbr_neutral_literal", "roubou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "rompiste": ("edit_ptbr_neutral_literal", "rompeu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "sentiste": ("edit_ptbr_neutral_literal", "sentiu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "sometiste": ("edit_ptbr_neutral_literal", "submeteu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "tiraste": ("needs_context", "", "tirar can mean atirou/tirou/removeu depending on sentence context"),
    "torturaste": ("edit_ptbr_neutral_literal", "torturou", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "transferiste": ("edit_ptbr_neutral_literal", "transferiu", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "trajiste": ("edit_ptbr_neutral_literal", "trouxe", "regular Spanish verb mapped to neutral PT-BR past tense"),
    "uniste": ("edit_ptbr_neutral_literal", "uniu", "regular Spanish verb mapped to neutral PT-BR past tense"),
}


def latest_sample(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    candidates = sorted(reports_dir.glob(DEFAULT_SAMPLE_GLOB), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("No Select_CString preterite validation sample found.")
    return candidates[0]


def output_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_select_cstring_preterite_ptbr_assisted_review"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"Line {line_number} is not a JSON object.")
        rows.append(payload)
    return rows


def review_row(row: dict[str, Any]) -> dict[str, Any]:
    left = str(row.get("left_literal") or "").strip()
    left_key = left.lower()
    suggested = str(row.get("suggested_ptbr_neutral_literal") or "").strip()
    sample_reason = str(row.get("sample_reason") or "")
    if suggested:
        decision = "approve_ptbr_neutral_literal"
        approved = suggested
        reason = "existing high-confidence PT-BR neutral hint approved as learning evidence"
    else:
        decision, approved, reason = PTBR_VERB_MAP.get(
            left_key,
            ("needs_context", "", "no safe local PT-BR mapping yet; requires context or human review"),
        )
    confidence = "positive" if decision in {"approve_ptbr_neutral_literal", "edit_ptbr_neutral_literal"} else "boundary"
    return {
        "queue_item_id": row.get("queue_item_id"),
        "ledger_item_id": row.get("ledger_item_id"),
        "segment_id": row.get("segment_id"),
        "target_agent": row.get("target_agent") or TARGET_AGENT,
        "left_literal": left,
        "right_literal": row.get("right_literal"),
        "sample_reason": sample_reason,
        "decision": decision,
        "approved_ptbr_neutral_literal": approved,
        "confidence_label": confidence,
        "review_reason": reason,
        "reviewer": "codex_select_cstring_preterite_assisted",
        "learning_only": 1,
        "apply_allowed": 0,
        "production_release_allowed": 0,
    }


def write_outputs(*, report_path: Path, decisions_path: Path, sample_path: Path, rows: list[dict[str, Any]]) -> None:
    reviewed = [review_row(row) for row in rows]
    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in reviewed:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    decision_counts = Counter(row["decision"] for row in reviewed)
    confidence_counts = Counter(row["confidence_label"] for row in reviewed)
    reason_counts = Counter(row["sample_reason"] for row in reviewed)
    verb_counts = Counter(str(row["left_literal"]).lower() for row in reviewed)
    lines = [
        "Select_CString preterite PT-BR assisted review",
        f"Rule version: {RULE_VERSION}",
        f"Source sample: {sample_path}",
        "",
        "Summary:",
        f"- Rows reviewed: {len(reviewed):,}",
        f"- Positive learning evidence: {confidence_counts['positive']:,}",
        f"- Boundary/context evidence: {confidence_counts['boundary']:,}",
        "- Learning only: 1",
        "- Apply allowed: 0",
        "- Production release allowed: 0",
        "",
        "Decisions:",
        *[f"- {key}: {value:,}" for key, value in decision_counts.most_common()],
        "",
        "Sample reasons:",
        *[f"- {key}: {value:,}" for key, value in reason_counts.most_common()],
        "",
        "Verbs:",
        *[f"- {key}: {value:,}" for key, value in verb_counts.most_common()],
        "",
        "Boundary/context rows:",
    ]
    for row in reviewed:
        if row["confidence_label"] == "boundary":
            lines.append(f"- {row['left_literal']!r} -> {row['right_literal']!r}: {row['review_reason']}")
    lines.extend(["", "Positive samples:"])
    for row in [r for r in reviewed if r["confidence_label"] == "positive"][:30]:
        lines.append(
            f"- {row['left_literal']!r} -> {row['right_literal']!r}: "
            f"{row['approved_ptbr_neutral_literal']!r} ({row['decision']})"
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, sample_jsonl: str | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    selected_sample = Path(sample_jsonl) if sample_jsonl else latest_sample(settings)
    rows = load_jsonl(selected_sample)
    report_path, decisions_path = output_paths(settings)
    write_outputs(report_path=report_path, decisions_path=decisions_path, sample_path=selected_sample, rows=rows)
    print("[issue_select_cstring_preterite_ptbr_assisted_review] Review generated")
    print(f"[issue_select_cstring_preterite_ptbr_assisted_review] Rule version: {RULE_VERSION}")
    print(f"[issue_select_cstring_preterite_ptbr_assisted_review] Rows: {len(rows):,}")
    print(f"[issue_select_cstring_preterite_ptbr_assisted_review] Report: {report_path}")
    print(f"[issue_select_cstring_preterite_ptbr_assisted_review] Decisions: {decisions_path}")
    return {"rows": len(rows), "report_path": str(report_path), "decisions_path": str(decisions_path)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create learning-only assisted decisions for Select_CString preterite PT-BR sample.")
    parser.add_argument("--sample-jsonl", default=None)
    args = parser.parse_args()
    main(sample_jsonl=args.sample_jsonl)
