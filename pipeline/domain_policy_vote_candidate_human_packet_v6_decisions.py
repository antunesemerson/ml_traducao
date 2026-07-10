from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_human_packet_v6_human_decisions"
PACKET_JSONL = Path("reports/20260628_220851_494801_domain_policy_vote_candidate_human_packet.jsonl")
SEGMENT_STATE_RUN_ID = 491

CORRECTIONS: dict[int, str] = {
    240991: "da Sombra",
    239262: "Governante feudal se convertendo a uma fé não reformada",
    237936: "Permite que adeptos divinem as estrelas e prevejam seu futuro",
    240134: "Desde que não sejam parentes diretos, membros da família podem se casar entre si pelo bem da dinastia.",
    239985: "O amor transcende o físico e se torna algo tão profundo e maravilhoso que não pode ser restringido pelo mero gênero.",
    240227: "Cuidado com o falso profeta que distorce e subverte a Fé para seus próprios fins. Aqueles que o fazem são hereges e devem ser destruídos.",
    240030: "O espiritual e o físico estão intrinsecamente ligados, completos e inseparáveis. É tolo ter dois líderes diferentes para um único reino.",
    101372: "O corpo deve ser enterrado em algum lugar dentro de seu próprio reino. Locais sagrados e edifícios religiosos melhorarão a qualidade da cerimônia.",
    237798: "Nossa fé e nossa cultura estão intrinsecamente ligadas. Esta prática fortalece os laços de nossa comunidade, embora tenda a excluir os estrangeiros.",
    238023: "Embora não concordemos com suas afirmações sobre o povo escolhido de Deus, a sabedoria da Torá judaica é inegável e há muito a ser aprendido com o texto.",
    241086: "Um deus supremo reina sobre tudo, mas está intimamente ligado ao Sol e à Lua, às montanhas e aos vales, e a toda a natureza que nos cerca. Por meio da veneração a deus e aos espíritos, podemos entrar em comunhão com a própria terra.",
    237970: "Há algo a ser aprendido com a maneira como as diferentes religiões dhármicas se toleram. Elas podem não seguir a verdadeira fé, mas isso não as torna más.",
    237750: "Os prazeres da boa comida e bebida só servem para nos distrair do serviço ao divino. Isolar-nos em monastérios remotos é a melhor maneira de evitar essas tentações.",
    238080: "Demônios andam pela terra sob o disfarce de piedosos e devotos; quem matar um dará um passo em direção ao estado búdico, quem matar dez dará dez passos em direção ao estado búdico.",
    237935: "O céu noturno é nossa janela para o reino do divino. Os grandes segredos e mistérios do mundo estão escritos ali, abertos para todos aprenderem se souberem onde olhar.",
    240510: "Normalmente definidas por reverência à natureza, veneração aos ancestrais e crença nos espíritos, muitas fés nativas são consideradas várias formas de paganismo.",
    240035: "Como o casamento é uma união sagrada, ter múltiplos casamentos é claramente mais sagrado do que ter apenas um casamento, contanto que cada casamento seja sagrado em si mesmo.",
    238043: "Mentir é a arte mais profunda que existe. Aqueles que conseguem convencer os outros de suas mentiras são santos. Aqueles que conseguem mentir para si mesmos com sucesso são imparáveis.",
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
    output_path = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_human_packet_v6_decisions.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"decisions={output_path}")
    print(f"review_count={payload['review_count']}")
    print(f"approve_already_ok_count={payload['approve_already_ok_count']}")
    print(f"approve_correction_count={payload['approve_correction_count']}")
    print("apply_ready_now=0")
    print("production_full_recommended_now=false")


if __name__ == "__main__":
    main()
