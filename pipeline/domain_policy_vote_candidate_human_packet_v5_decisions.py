from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_human_packet_v5_human_decisions"
PACKET_JSONL = Path("reports/20260628_203239_837581_domain_policy_vote_candidate_human_packet.jsonl")
SEGMENT_STATE_RUN_ID = 490

CORRECTIONS: dict[int, str] = {
    63081: "O califa mantém a Pérsia sob controle férreo, promovendo uma fé repleta de ortodoxia árabe. Acredito que a região precisa de sua própria identidade, e há muitas opções para escolher...",
    63082: "O califa mantém a Pérsia sob controle férreo, promovendo uma fé repleta de ortodoxia árabe. Acredito que a região precisa de sua própria identidade, e há muitas opções para escolher...",
    63498: "Este personagem não tolerará nenhuma artimanha no condado que lhe foi designado.",
    62935: "Reunir apoiadores para reivindicar uma parte das terras do seu suserano",
    69449: '"O Império encontraria utilidade em regimentos dos meus melhores soldados. Concorde conosco, e eles estarão sob seu comando."',
    63110: "Numerosos colonos religiosos estão afluindo para este condado vindos de todo o mundo. Sua riqueza, poder e fervor estão se revelando uma grande bênção para a região.",
    63496: "Houve uma tentativa de construir uma grande e nova dakhma neste condado, mas erros foram cometidos na construção, e matéria cadavérica putrefata está envenenando as águas subterrâneas.",
    64845: "Você sugere estudar os projetos das armas de cerco, procurando uma fraqueza ou até mesmo uma inovação",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_packet_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with PACKET_JSONL.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def build_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        segment_id = int(row["segment_id"])
        corrected_text = CORRECTIONS.get(segment_id, row["current_output_text"])
        decision = "approve_correction" if segment_id in CORRECTIONS else "approve_already_ok"
        records.append(
            {
                "packet_index": int(row["packet_index"]),
                "segment_id": segment_id,
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": int(row["source_line_number"]),
                "english_text": row["english_text"],
                "spanish_text": row["spanish_text"],
                "current_output_text": row["current_output_text"],
                "corrected_text": corrected_text,
                "decision": decision,
            }
        )
    return records


def main() -> None:
    records = build_records(load_packet_rows())
    counts = Counter(record["decision"] for record in records)
    payload = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "packet_jsonl": str(PACKET_JSONL),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "review_count": len(records),
        "approve_already_ok_count": counts.get("approve_already_ok", 0),
        "approve_correction_count": counts.get("approve_correction", 0),
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "next_action": "controlled_decision_ingest_then_protected_apply_for_corrections",
        "records": records,
    }
    output_path = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_human_packet_v5_decisions.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"decisions={output_path}")
    print(f"review_count={payload['review_count']}")
    print(f"approve_already_ok_count={payload['approve_already_ok_count']}")
    print(f"approve_correction_count={payload['approve_correction_count']}")
    print("apply_ready_now=0")
    print("production_full_recommended_now=false")


if __name__ == "__main__":
    main()
