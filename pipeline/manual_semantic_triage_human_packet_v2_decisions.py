from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "manual_semantic_triage_human_packet_v2_decisions"
PACKET_JSONL = Path("reports/20260628_175812_519261_manual_semantic_triage_human_packet.jsonl")
SEGMENT_STATE_RUN_ID = 485
LEDGER_RUN_ID = 76

CORRECTIONS: dict[int, str] = {
    283020: "As vastas [GetBuilding('ijil_mines_01').GetName|l] abrigam ricos depósitos de sal. Os habilidosos mineiros tornaram esta área uma contribuição crucial para o comércio e o desenvolvimento da região.",
    283001: "As [GetBuilding('argentiera_mines_01').GetName|l] são famosas por seus ricos veios de chumbo e prata. As condições severas enfrentadas pelos mineiros aqui ressaltam a busca implacável por esses valiosos recursos.",
    283059: "O [GetBuilding('patras_castle').GetName|l] tem servido como sentinela ao longo dos séculos. Suas robustas muralhas e torres, marcadas por várias épocas, são um testemunho da importância histórica e do valor estratégico da cidade.",
    283026: "Desde os tempos antigos, os mineiros do [GetBuilding('fengtian_mines_01').GetName|l] têm brandido suas picaretas para ampliar as escavações e reunir jade para inspeção. Enquanto isso, os comerciantes da ilha encheram seus cofres.",
    64590: "Contra os grandes poderes que desprezam os fracos, toda alma [ROOT.Char.GetCulture.GetName] que lutar sozinha morrerá sozinha.\\n\\nPreciso encontrar aqueles que amam meu povo e jurar um voto sagrado de lutar juntos até a morte.",
    283167: "Quando chego, o [GetBuilding('swahili_port_zanzibar').GetName|l] fervilha de atividade sob nuvens à deriva, com portas de madeira entalhadas levando a becos cheios de barracas de especiarias e navios estrangeiros atracando no porto.",
    283005: "As [GetBuilding('zawar_mines_01').GetName|l] são uma antiga fonte de zinco e chumbo. A engenhosidade dos mineiros, que mergulham nas profundezas para extrair esses metais, tem sido crucial para sustentar o crescimento e o desenvolvimento da região.",
    135346: "O ar está fresco quando ponho a cabeça para fora, olhando para o vale abaixo de [holding_location.GetName]. Aprecio a diferença de temperatura enquanto observo a vida selvagem e noto um grupo de viajantes avançando diligentemente pelo vale.",
    283087: "O [GetBuilding('jokhang_01').GetName|l] é um complexo labiríntico de pátios e residências monásticas atravessado por corredores cavernosos e salões cobertos com manteiga de iaque. Hm, manteiga de iaque, talvez eu devesse experimentar um pouco...",
    282996: "As [GetBuilding('falun_mines_01').GetName|l] são um labirinto de túneis em busca de cobre. O vibrante pigmento vermelho, o vermelho de Falun, derivado das minas, adorna muitos edifícios aqui, um vívido lembrete da importância da mina para a comunidade local.",
    283090: "A magnífica [GetBuilding('holy_wisdom_01').GetName] foi modelada com base na Hagia Sophia, para representar a mesma luz, riqueza e poder na terra gélida do norte. Sua aparência austera de calcário branco contrasta com as cores vibrantes dos afrescos em seu interior.",
    283046: "Os [GetBuilding('cilician_gates').GetName] têm sido uma encruzilhada estratégica por séculos. Este corredor natural, utilizado por comerciantes e exércitos, possui grande significado para as comunidades locais que há muito percorrem esses caminhos.",
    135347: "A suave inclinação das colinas ao redor de [holding_location.GetName] é agradável aos olhos enquanto olho para fora e, lá embaixo, vejo o pequeno lago. Um grupo de viajantes parece contornar o lago, seguindo a passos largos em direção ao seu destino.",
    78683: "Com hesitação, reconsidero meus planos de partir para [ongoing_destination.GetName] para realizar os rituais.\\n\\nSerá que este é realmente o momento de viajar? Meu tempo não seria melhor aproveitado em outro lugar? Reflexões importantes para um ícone religioso.",
    283060: "Esculpido nas falésias de Trabzon, o [GetBuilding('sumela_monastery_01').GetName|l] é uma maravilha da arquitetura monástica. Este retiro remoto, agarrado à encosta da montanha, oferece vistas deslumbrantes e uma sensação de paz profunda àqueles que o visitam.",
    282982: "O [GetBuilding('dome_of_the_rock_01').GetName|l], com sua cúpula dourada brilhando ao sol, é um marco reverenciado em Jerusalém. Este santuário, que abriga a rocha sagrada, é um local de profunda importância espiritual, reverenciado por muçulmanos e judeus.",
    282955: "A [GetBuilding('visby_ringmur_01').GetName] circunda a cidade, um testemunho de pedra de sua importância para o comércio e a defesa. Caminhando por essas muralhas, admiro a habilidade dos construtores e a visão dos líderes, que garantiram a segurança e a prosperidade de seu povo.",
    282935: "Diz-se que o [GetBuilding('gunung_api_01').GetName] é uma montanha sagrada, e sua aparência certamente é diferente de qualquer outra que encontrei em minhas viagens. Rochas finas e pontiagudas se erguem abruptamente no ar, fazendo as árvores ao redor parecerem pequenas.",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_packet_rows() -> list[dict[str, Any]]:
    with PACKET_JSONL.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def build_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id in CORRECTIONS:
            decision = "approve_correction"
            corrected_text = CORRECTIONS[segment_id]
        else:
            decision = "approve_already_ok"
            corrected_text = row["current_output_text"]
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
                "hold_reason": "",
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
        "hold_segment_ids": [],
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "next_action": "controlled_decision_ingest_then_protected_apply_dry_run",
        "records": records,
    }
    output_path = reports_dir() / f"{stamp()}_manual_semantic_triage_human_packet_v2_decisions.json"
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
