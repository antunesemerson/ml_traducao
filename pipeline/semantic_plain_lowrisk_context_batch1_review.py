from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "semantic_plain_lowrisk_context_batch1_review_v1"
TARGET_DOMAINS = {"traits", "general"}

CORRECTIONS = {
    281301: "O Guerreiro Desorientado recebeu educação na arte da guerra, mas tem mais chance de ferir a si próprio do que a seus inimigos.",
    31125: "\"Você sabe que nossa amizade é muito importante para mim e que não desejo outra coisa além de ouvir seus conselhos. Eu lhe peço, sente-se ao meu lado.",
    31889: "\n\nEmbora eles tenham imposto um alto preço à nossa família nos anos seguintes, não há maior satisfação ou alívio do que saber que sua bandeira nunca mais será hasteada.",
    33078: "Passe um semestre em um renomado centro de estudos, aprendendo com as mentes mais sábias da região.\n\nÉ uma ocasião para preencher as lacunas em sua educação juvenil, mas também para se aprimorar como um todo, desenvolver suas habilidades, aprender novas capacidades e fazer contatos importantes.",
}

HOLD_RE = re.compile(r"Negociador Adequado|Guerreiro Desorientado", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_review_packet() -> Path:
    matches = sorted(
        reports_dir().glob("*_semantic_plain_lowrisk_human_review_packet.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit("missing semantic_plain_lowrisk_human_review_packet jsonl")
    return matches[0]


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("initial_classification") == "needs_more_context" and row.get("source_domain") in TARGET_DOMAINS:
                rows.append(row)
    return rows


def classify(row: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    current = str(row.get("current_output_text") or "")
    corrected = CORRECTIONS.get(segment_id, "")
    if segment_id == 281274:
        decision = "hold_trait_term_consistency"
        rationale = "trait name likely needs glossary/term consistency before changing"
    elif segment_id == 281301:
        decision = "human_correction_candidate"
        rationale = "minor PT-BR naturalness; preserve trait title"
    elif corrected:
        decision = "human_correction_candidate"
        rationale = "human-readable fluency correction candidate; no CK3 tokens"
    elif HOLD_RE.search(current):
        decision = "hold_trait_term_consistency"
        rationale = "possible trait/title term consistency"
    else:
        decision = "needs_human_context_review"
        rationale = "requires user semantic/context decision"
    return {
        "segment_id": segment_id,
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "source_domain": row.get("source_domain"),
        "decision": decision,
        "rationale": rationale,
        "current_output_text": current,
        "corrected_text": corrected,
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "token_guard_ok": not any(marker in current or marker in corrected for marker in ("[", "]", "$", "#", "Select_CString", ".Custom('ES_")),
        "requires_apply_later": False,
        "requires_learning_if_approved": bool(corrected),
    }


def build_summary(input_path: Path, reviewed: list[dict[str, Any]]) -> dict[str, Any]:
    decision_counts = Counter(row["decision"] for row in reviewed)
    correction_candidates = sum(1 for row in reviewed if row["corrected_text"])
    token_guard_failures = sum(1 for row in reviewed if not row["token_guard_ok"])
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_packet": str(input_path),
        "target_domains": sorted(TARGET_DOMAINS),
        "reviewed_count": len(reviewed),
        "decision_counts": [{"key": key, "count": value} for key, value in decision_counts.most_common()],
        "correction_candidate_count": correction_candidates,
        "token_guard_failure_count": token_guard_failures,
        "reviewed": reviewed,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "next_action": "human_review_batch1_corrections_and_holds",
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_plain_lowrisk_context_batch1_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in summary["reviewed"]:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "semantic plain lowrisk context batch1 review",
        f"source={SOURCE}",
        f"input_packet={summary['input_packet']}",
        f"target_domains={','.join(summary['target_domains'])}",
        f"reviewed_count={summary['reviewed_count']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"correction_candidate_count={summary['correction_candidate_count']}",
            f"token_guard_failure_count={summary['token_guard_failure_count']}",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"next_action={summary['next_action']}",
            "",
            "reviewed:",
        ]
    )
    for row in summary["reviewed"]:
        lines.append(f"- {row['segment_id']} | {row['decision']} | {row['rationale']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    input_path = latest_review_packet()
    rows = read_rows(input_path)
    reviewed = [classify(row) for row in rows]
    summary = build_summary(input_path, reviewed)
    txt_path, jsonl_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"reviewed_count={summary['reviewed_count']}")
    print(f"correction_candidate_count={summary['correction_candidate_count']}")
    print(f"token_guard_failure_count={summary['token_guard_failure_count']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
