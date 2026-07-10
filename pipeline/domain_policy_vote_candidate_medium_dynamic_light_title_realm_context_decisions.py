from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


PACKET_PATH = Path("reports/20260629_150024_804458_domain_policy_vote_candidate_medium_dynamic_light_title_realm_context_human_packet.jsonl")
SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_title_realm_context_decisions"

DECISIONS: dict[int, tuple[str, str, str]] = {
    139877: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    139675: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    157176: ("needs_more_context", "", "Preposition with landed title expansion needs context."),
    35290: ("needs_more_context", "", "Faith name preposition/article may depend on token expansion."),
    157301: ("needs_more_context", "", "Preposition with landed title expansion needs context."),
    52168: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    52513: (
        "approve_correction",
        "\"Que aventura!\", exclama [financier.GetFirstNameNoTooltip]. \"Quem poderia afirmar ter coragem igual à nossa, que tomamos este grande império?\"",
        "Fix quotation style and literal phrasing.",
    ),
    163580: ("needs_more_context", "", "Preposition with previous ruler/name context should stay held."),
    100522: (
        "approve_correction",
        "Convido você para meu banquete em [barony.GetNameNoTier]! Será um evento grandioso, com comida deliciosa e entretenimento.",
        "Remove unsupported court insertion and improve invitation wording.",
    ),
    64281: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    121487: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    287750: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    22691: (
        "approve_correction",
        "Apesar de estar mais interessado em rumores sobre o povo do reino do que em seus impostos e censos, [ROOT.Char.GetFirstName] desenvolveu aptidão para administração.",
        "Fix preposition and plural census phrasing.",
    ),
    157173: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    157584: (
        "approve_already_ok",
        "",
        "Current text already matches the approved wording.",
    ),
    116143: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    38021: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    127348: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    119566: (
        "approve_correction",
        "Colinas e vales, planícies e lagos, estradas e caminhos florestais. Já vi tudo isso no reino de [painting_land_owner.GetTitledFirstName|l], e meus cartógrafos têm feito anotações e esboços a carvão de cada nova paisagem.",
        "Fix broken preposition and improve final clause.",
    ),
}


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    by_id = {int(row["segment_id"]): row for row in read_jsonl(PACKET_PATH)}
    missing = sorted(set(DECISIONS) - set(by_id))
    if missing:
        raise SystemExit(f"decision segment ids missing from packet: {missing}")
    rows: list[dict[str, Any]] = []
    for segment_id in sorted(DECISIONS, key=lambda sid: int(by_id[sid]["packet_index"])):
        decision, corrected_text, notes = DECISIONS[segment_id]
        rows.append({**by_id[segment_id], "decision_source": SOURCE, "human_decision": decision, "corrected_text": corrected_text, "review_notes": notes})
    counts = Counter(row["human_decision"] for row in rows)
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_human_decisions",
        "decision_source": SOURCE,
        "source_packet": str(PACKET_PATH),
        "decision_count": len(rows),
        "decision_counts": [{"key": key, "count": count} for key, count in counts.most_common()],
        "gates": {"apply": "not_run", "lifecycle": "not_run", "segment_state": "not_run", "reindex": "not_run", "full_production": "not_run"},
        "output_files": {},
    }
    base = reports_dir() / f"{stamp()}_{SOURCE}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["domain_policy_vote_candidate medium_dynamic_light title realm context decisions", "", f"decision_count: {len(rows)}", "", "decision_counts:"]
    lines.extend(f"- {count} | {key}" for key, count in counts.most_common())
    for row in rows:
        lines.extend(["", f"## segment_id {row['segment_id']}", f"- human_decision: {row['human_decision']}", f"- current_output_text: {row.get('current_output_text')}", f"- corrected_text: {row.get('corrected_text') or ''}", f"- review_notes: {row.get('review_notes') or ''}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
