from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


PACKET_PATH = Path("reports/20260629_161035_988570_domain_policy_vote_candidate_medium_dynamic_light_faith_route_review.jsonl")
SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_faith_context_decisions"

DECISIONS: dict[int, tuple[str, str, str]] = {
    239225: (
        "approve_correction",
        "Administrador [ganges_faith.GetAdherentName] do Ganges",
        "Steward is an administrator/guardian role here, not treasurer.",
    ),
    239239: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    240368: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    240177: ("approve_already_ok", "", "Current PT-BR plural agreement is acceptable."),
    240187: (
        "approve_correction",
        "Qualquer pessoa que tenha sentido o chamado divino deve ser capaz de servir como [ROOT.Faith.PriestNeuterPlural], independentemente de seu gênero.",
        "Remove singular article before plural clergy token.",
    ),
    237775: ("needs_more_context", "", "PantheonTerm can expand as singular or plural; verb agreement needs context/policy."),
    238052: ("needs_more_context", "", "PantheonTerm can expand as singular or plural; verb agreement needs context/policy."),
    238057: ("approve_already_ok", "", "HighGodName is singular and current agreement is acceptable."),
    239242: (
        "approve_correction",
        "Reformas religiosas de [theravada_faith.GetName|l]",
        "Add safe preposition without forcing article contraction.",
    ),
    163502: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    163503: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    62252: (
        "approve_correction",
        "não conseguiu fazer com que [unity_target.GetName] se juntasse a uma ordem sagrada",
        "Improve fluency and tense agreement.",
    ),
    157182: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    157588: (
        "approve_correction",
        "Pedir a [recipient.GetShortUINameNoTooltip] que faça os votos e se junte a uma Ordem Sagrada",
        "Improve regency and fluency.",
    ),
    239197: (
        "approve_correction",
        "Nova Propriedade de Templo em [temple_location.GetName]",
        "Holding is not a title holder; use property/holding sense.",
    ),
    239222: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    285245: (
        "approve_correction",
        "Eles se comprometeram como atacantes na [GREAT_HOLY_WAR.GetName] da sua fé",
        "Fix agreement and make phrasing natural.",
    ),
}


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
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
    for segment_id in sorted(DECISIONS):
        decision, corrected_text, notes = DECISIONS[segment_id]
        rows.append(
            {
                **by_id[segment_id],
                "decision_source": SOURCE,
                "human_decision": decision,
                "corrected_text": corrected_text,
                "review_notes": notes,
            }
        )
    counts = Counter(row["human_decision"] for row in rows)
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_human_decisions",
        "decision_source": SOURCE,
        "source_packet": str(PACKET_PATH),
        "decision_count": len(rows),
        "decision_counts": [{"key": key, "count": count} for key, count in counts.most_common()],
        "gates": {
            "apply": "not_run",
            "lifecycle": "not_run",
            "segment_state": "not_run",
            "reindex": "not_run",
            "full_production": "not_run",
        },
        "output_files": {},
    }
    base = reports_dir() / f"{stamp()}_{SOURCE}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light faith context decisions",
        "",
        f"decision_count: {len(rows)}",
        "",
        "decision_counts:",
    ]
    lines.extend(f"- {count} | {key}" for key, count in counts.most_common())
    for row in rows:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']}",
                f"- human_decision: {row['human_decision']}",
                f"- current_output_text: {row.get('current_output_text')}",
                f"- corrected_text: {row.get('corrected_text') or ''}",
                f"- review_notes: {row.get('review_notes') or ''}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
