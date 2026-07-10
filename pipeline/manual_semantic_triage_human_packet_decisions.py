from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "manual_semantic_triage_human_packet_decisions_v1"
PACKET_JSONL = Path("reports/20260628_170528_669487_manual_semantic_triage_human_packet.jsonl")
SEGMENT_STATE_RUN_ID = 484
LEDGER_RUN_ID = 76

HOLDS = {
    112295: "fragment_needs_previous_context",
    162872: "fragment_needs_previous_context",
}

CORRECTIONS: dict[int, str] = {
    120831: "[ROOT.Char.GetFullName], o Magnânimo!",
    120875: "…$nick_the_chronicler$!",
    120873: "…$nick_the_enlightened$!",
    120880: 'Hm. Que tal "$nick_the_scholar$"?',
    120879: 'Quem sou eu além de "$nick_the_sage$"?',
    163875: "[sex_partner.GetFirstName] e eu fomos para a cama juntos",
    163876: "você e [sex_partner.GetFirstName] foram para a cama juntos",
    12106: '"Ouvi dizer que o [task_contract_destination.GetHolding.GetSpecialBuildingType.GetName]',
    101653: "[CHARACTER.GetName] realizou um damm para compensar a negligência no rito de Safa e Marwah",
    101655: "[CHARACTER.GetName] realizou uma Sadaqah al-Fitr para compensar a negligência no rito de Safa e Marwah",
    165334: "[owner.GetName] viu uma fera ser despedaçada por seu predador na natureza enquanto ela ia ao encontro de seu par",
    157259: "Por meio desta, concedo a você [landed_title.GetName], sua posição, território formal e renda associada. Que você governe bem!",
    165081: "Esmaguei a coalizão liderada por [opponent.GetName], não deixando ninguém para se opor a que eu me tornasse o Maior de Todos os Cãs",
    62620: "Logo, todo [ROOT.Char.GetFaith.GetReligion.GetName|l] renunciará às suas falsas convicções e se unirá à nossa justa causa — ou morrerá!",
    102039: "Dou uma olhada em [accomplice.GetFirstName], que assente quase imperceptivelmente, com a testa franzida de determinação, aguardando meu sinal.",
    163767: "minha alma gêmea [childless_soulmate.GetName] e eu refletimos sobre o fato de que pudemos compartilhar a vida juntos, ainda que não um filho",
    163768: "você e sua alma gêmea [childless_soulmate.GetName] refletiram sobre o fato de que puderam compartilhar a vida juntos, ainda que não um filho",
    58192: '"Aqueles monólitos loucamente retorcidos, as antigas torres de fadas de [current_location.GetName]: seus buracos abrigam portas trancadas", murmuram meus companheiros.',
    66541: "Um rebanho selvagem tem devastado os campos ao redor de [TaskContract.GetLocation.GetName]. Nossos próprios rebanhos sofrerão enquanto essas bestas continuarem soltas.",
    100757: "\n\n[potential_friend.GetFirstName] e eu acabamos conversando a noite toda, e concordamos que aquela não deveria ser a última vez que festejamos e rimos na companhia um do outro!",
    160099: "[char_current.GetFirstName] nos leva à fonte de sua riqueza: uma armeria bem abastecida — com forja anexa — que fornece armas e munição tanto para a milícia quanto para caravanas de passagem.",
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
        if segment_id in HOLDS:
            decision = "needs_more_context"
            corrected_text = ""
            hold_reason = HOLDS[segment_id]
        elif segment_id in CORRECTIONS:
            decision = "approve_correction"
            corrected_text = CORRECTIONS[segment_id]
            hold_reason = ""
        else:
            decision = "approve_already_ok"
            corrected_text = row["current_output_text"]
            hold_reason = ""
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
                "hold_reason": hold_reason,
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
        "ledger_run_id": LEDGER_RUN_ID,
        "review_count": len(records),
        "approve_already_ok_count": counts.get("approve_already_ok", 0),
        "approve_correction_count": counts.get("approve_correction", 0),
        "needs_more_context_count": counts.get("needs_more_context", 0),
        "hold_segment_ids": sorted(HOLDS),
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "next_action": "controlled_decision_ingest_excluding_holds_then_protected_apply_dry_run",
        "records": records,
    }
    output_path = reports_dir() / f"{stamp()}_manual_semantic_triage_human_packet_decisions.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"decisions={output_path}")
    print(f"review_count={payload['review_count']}")
    print(f"approve_already_ok_count={payload['approve_already_ok_count']}")
    print(f"approve_correction_count={payload['approve_correction_count']}")
    print(f"needs_more_context_count={payload['needs_more_context_count']}")
    print("apply_ready_now=0")
    print("production_full_recommended_now=false")


if __name__ == "__main__":
    main()
