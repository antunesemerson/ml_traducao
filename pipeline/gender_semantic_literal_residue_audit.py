from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE_LANE = "literal_residue_candidate_human_review"

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|\.Custom\('ES_[A-Za-z0-9_]+'\)"
)
SPANISH_WORD_RE = re.compile(
    r"\b(?:muchos|muchas|mucho|mucha|verdadero|verdadera|seg[uú]n|probabilidad|mientras|"
    r"l[aá]rgate|ah[oó]rranos|personaje|hach[ií]s)\b",
    re.IGNORECASE,
)


EXPLICIT_CANDIDATES: dict[int, tuple[str, str]] = {
    36200: (
        "Todas as manhãs ele se levanta para treinar, brandindo sua espada de um lado para o outro.\\n\\n"
        "Até esta mesma noite, quando os outros estão ocupados apreciando a comida, "
        "[lifestyle_character.GetSheHe] saiu por conta própria para fazer exercícios que inspiram admiração. "
        "Não consigo evitar querer emular partes de seu estilo de vida.",
        "remove duplicated hardcoded pronoun after preserved GetSheHe token",
    ),
    281408: (
        "Este personagem é conhecido por sua abordagem pouco convencional, porém prática, da religião.",
        "normalize generic trait description to established masculine-generic 'Este personagem'",
    ),
    281427: (
        "Este personagem gosta de passar as noites dando rima e verso a seus pensamentos, sentimentos e experiências.",
        "normalize generic trait description to established masculine-generic 'Este personagem'",
    ),
    281445: (
        "Este personagem mantém mão de ferro sobre sua bolsa e está sempre procurando formas de enchê-la ainda mais.",
        "normalize generic trait description to established masculine-generic 'Este personagem'",
    ),
    281562: (
        "Este personagem se entregou à bebida e está sempre procurando chegar ao fundo da garrafa.",
        "normalize generic trait description and repair awkward literal phrasing",
    ),
    281565: (
        "Este personagem depende do consumo de haxixe para lidar com o estresse da vida cotidiana.",
        "normalize generic trait description to established masculine-generic 'Este personagem'",
    ),
    281583: (
        "Este personagem costuma doar para caridade muito mais dinheiro do que pode razoavelmente bancar.",
        "normalize generic trait description to established masculine-generic 'Este personagem'",
    ),
    281586: (
        "Este personagem sofre com a consciência pesada, muitas vezes sentindo-se compelido a confessar coisas que era melhor deixar sem menção.",
        "normalize generic trait description and adjectival agreement",
    ),
}

CONTEXT_BLOCKS: dict[int, str] = {
    45251: "multiple gender getters are embedded in prose; 'ver[GetHerHim]' and opening GetSheHe need policy/context before repair",
    68698: "concept pipe forms such as [minister|El] are protected CK3 concept tokens; changing them requires concept-token policy",
}

FALSE_POSITIVES: dict[int, str] = {
    37421: "'Está claro' and surrounding text are valid PT-BR; no literal Spanish residue found",
    37703: "'sua presença' and surrounding text are valid PT-BR; no literal Spanish residue found",
}


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


def audit(row: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    current = str(row.get("current_output_text") or "")
    candidate = ""
    if segment_id in EXPLICIT_CANDIDATES:
        candidate, reason = EXPLICIT_CANDIDATES[segment_id]
        token_integrity_ok = token_inventory(current) == token_inventory(candidate)
        structure_integrity_ok = current.count("\n") == candidate.count("\n") and current.count("#") == candidate.count("#")
        decision = "candidate_needs_human_approval" if token_integrity_ok and structure_integrity_ok else "blocked_integrity_guard"
        safe_for_future_apply_batch = False
    elif segment_id in CONTEXT_BLOCKS:
        reason = CONTEXT_BLOCKS[segment_id]
        decision = "blocked_context_required"
        token_integrity_ok = True
        structure_integrity_ok = True
        safe_for_future_apply_batch = False
    elif segment_id in FALSE_POSITIVES:
        reason = FALSE_POSITIVES[segment_id]
        decision = "false_positive_no_change"
        token_integrity_ok = True
        structure_integrity_ok = True
        safe_for_future_apply_batch = False
    else:
        reason = "unclassified literal residue candidate; keep for human review"
        decision = "needs_human_review_unclassified"
        token_integrity_ok = False
        structure_integrity_ok = False
        safe_for_future_apply_batch = False

    return {
        **row,
        "audit_source": "gender_semantic_literal_residue_audit_v1",
        "audit_decision": decision,
        "audit_reason": reason,
        "candidate_text": candidate,
        "would_change_output": bool(candidate and candidate != current),
        "token_integrity_ok": token_integrity_ok,
        "structure_integrity_ok": structure_integrity_ok,
        "spanish_residue_after_candidate": bool(SPANISH_WORD_RE.search(candidate or current)),
        "safe_for_future_apply_batch": safe_for_future_apply_batch,
        "requires_human_approval": decision == "candidate_needs_human_approval",
        "requires_apply_later": decision == "candidate_needs_human_approval",
        "requires_lifecycle_later": False,
    }


def output_paths() -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_gender_semantic_literal_residue_audit"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), base.with_suffix(".csv"), reports_dir() / f"{base.name}_summary.json"


def write_outputs(records: list[dict[str, Any]], source_jsonl: str) -> tuple[Path, Path, Path, Path, dict[str, Any]]:
    txt_path, jsonl_path, csv_path, summary_path = output_paths()
    decisions = Counter(row["audit_decision"] for row in records)
    summary = {
        "schema_version": 1,
        "source": "gender_semantic_literal_residue_audit_v1",
        "input_package_jsonl": source_jsonl,
        "audited_count": len(records),
        "decision_counts": dict(sorted(decisions.items())),
        "candidate_needs_human_approval_count": decisions["candidate_needs_human_approval"],
        "safe_for_future_apply_batch_count": sum(1 for row in records if row["safe_for_future_apply_batch"]),
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "lifecycle_reindex_recommended_now": False,
        "next_action": "human_review_candidate_suggestions_then_generate_approved_packet",
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    fieldnames = [
        "segment_id",
        "relative_path",
        "source_key",
        "audit_decision",
        "audit_reason",
        "current_output_text",
        "candidate_text",
        "token_integrity_ok",
        "structure_integrity_ok",
        "requires_human_approval",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({key: record.get(key) for key in fieldnames})
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Gender semantic literal-residue audit",
        f"audited_count={summary['audited_count']}",
        "",
        "Decision counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(decisions.items()))
    lines.extend(
        [
            "",
            f"candidate_needs_human_approval_count={summary['candidate_needs_human_approval_count']}",
            "safe_for_future_apply_batch_count=0",
            "apply_ready_now=0",
            "Safety: read-only audit; no apply, no lifecycle/reindex, no training, no source/output edits.",
            f"next_action={summary['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, csv_path, summary_path, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-jsonl", required=True)
    args = parser.parse_args()
    source_path = db.project_path(args.package_jsonl)
    source_rows = [row for row in read_jsonl(source_path) if row.get("audit_lane") == SOURCE_LANE]
    records = [audit(row) for row in source_rows]
    txt_path, jsonl_path, csv_path, summary_path, summary = write_outputs(records, args.package_jsonl)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"csv={csv_path}")
    print(f"summary={summary_path}")
    print(f"audited_count={summary['audited_count']}")
    print("decision_counts=" + json.dumps(summary["decision_counts"], ensure_ascii=False, sort_keys=True))
    print(f"candidate_needs_human_approval_count={summary['candidate_needs_human_approval_count']}")
    print(f"safe_for_future_apply_batch_count={summary['safe_for_future_apply_batch_count']}")
    print("next_action=" + summary["next_action"])


if __name__ == "__main__":
    main()
