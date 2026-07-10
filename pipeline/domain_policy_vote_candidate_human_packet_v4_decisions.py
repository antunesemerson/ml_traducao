from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_human_packet_v4_human_decisions"
PACKET_JSONL = Path("reports/20260628_194125_655530_domain_policy_vote_candidate_human_packet.jsonl")
SEGMENT_STATE_RUN_ID = 489

CORRECTIONS: dict[int, str] = {
    21280: "Mesmo povos cavaleiros podem ser vencidos por cavalaria revestida da cabeça aos cascos em placas lamelares. Esta cultura não apenas desenvolveu gosto por tal armamento caro, mas emprega aqueles nascidos na sela, arco em mãos, como seus cavaleiros pesadamente armadurados.",
    20025: 'Esta cultura fez sua morada no "teto do mundo", onde o ar é fresco, mas frio, e os invernos são longos e rigorosos. Eles se orgulham de que poucas culturas das terras abaixo seriam capazes de viver tão bem quanto esse povo vive, entre picos e planaltos onde, de outro modo, apenas ovelhas podem prosperar.',
    20143: "Esta cultura é distintamente agrícola; suas terras produzem colheitas abundantes para exércitos famintos. Embora não ter que lutar pela comida signifique muitos camponeses para o recrutamento, também significa que cada soldado não é tão motivado quanto aqueles de regiões mais árduas.",
    21182: "O arroz é a força vital desta cultura. Por meio de trabalho diligente e conhecimento agrícola, suas colheitas são fartas enquanto controlarem terras adequadas para sua plantação. Como resultado, sua prosperidade econômica e seu poderio militar se elevam muito acima dos de seus vizinhos menos avançados.",
    20242: 'Aqueles que vivem no leste da África lembram-se bem dos grandes Reis Guerreiros que um dia enfrentaram invasores e forjaram reinos em Kush. Nossos vizinhos podem insistir na superioridade das mulheres, mas nós traçamos nossas linhagens por meio de nossos pais, e o grito de guerra dos poderosos homens núbios faz essas "mulheres" estrangeiras fugirem para casa.',
    20240: 'Aqueles que vivem no leste da África lembram-se bem das grandes Rainhas Guerreiras que um dia enfrentaram invasores e forjaram reinos em Kush. Nossos vizinhos podem insistir na superioridade dos homens, mas nós traçamos nossas linhagens por meio de nossas mães, e o grito de guerra das poderosas mulheres núbias faz esses "homens" estrangeiros fugirem para casa.',
    34199: 'falar com seus vassalos mais ecléticos, mantendo estável a diversidade de seu reino".',
    56394: "Este personagem estimulou a imaginação de seus súditos com promessas de estabilidade imperial.",
    56396: "Este personagem estimulou a imaginação de seus súditos com promessas de prosperidade imperial.",
    52940: "Um profeta autoproclamado está aterrorizando todo um condado com suas previsões apocalípticas.",
    34890: "Este personagem abandonou seus deveres de tutoria para se concentrar na administração do reino.",
    38337: "As pessoas deste baronato estão assustadas e irritadas com uma série de prisões de pregadores apocalípticos.",
    57909: "Este personagem recusou a oferta de seu suserano de um noivado para seu filho e decidiu procurar por conta própria.",
    56733: "Este condado está sem um grande carregamento de armas e armaduras, e a qualidade de suas tropas sofre como resultado.",
    38627: "Os godos causaram a destruição de Roma e fundaram nosso reino. Somos seus herdeiros, os reis e rainhas dos godos.",
    38623: "Cadell começou a vida como menino escravo e, com a bênção de um santo concedida a ele, arrancou o reino de Powys de Vortigern.",
    6396: "A Espada de Carlos Magno, forjada para conter a Lança de Longino. Joyeuse é um símbolo tanto do valor cavaleiresco quanto do legado real do império de Carlos Magno.",
    38629: "O príncipe varegue Rurik levou sua horda de aventureiros para o leste e estabeleceu o grande reino de Novgorod. É seu sangue lendário que corre em nossas veias.",
    53376: 'Um título honorífico que significa "venerável" ou "majestoso", concedido aos sucessores do primeiro imperador de Roma. No século VII, o título grego Basileus o substituiu em importância imperial.',
    38637: "O magnífico rei de Turan e herói dos turcos, Afrasiyab foi a nêmesis do Império Persa e o maior de todos os reis turanianos. Seu sangue mágico corre em nossas veias e nos dá o direito divino de governar Turan mais uma vez.",
    2252: "Monges treinados criam cuidadosamente mandalas de areia, que depois são destruídas ritualmente para transmitir a impermanência. Cada uma retrata uma divindade ou qualidade divina em seu núcleo, cercada por imagens harmonizadoras, refletindo como um reino mandala se organiza.",
    10432: "Este ninho de águia, erguido no alto de um penhasco elevado, foi construído pelos justânidas para servir como reduto fortificado de seu reino sitiado. Com seu cenário impressionante e fortes muralhas, é quase inexpugnável segundo todos os relatos, e pode se revelar uma fortaleza ideal para qualquer facção ou seita guerreira que prospere em um local tão isolado.",
    62399: "Os guerreiros desta cultura não estão presos à pompa e enfatizam o valor e a adaptabilidade do soldado comum em detrimento da elite montada tradicional.",
    16911: "Uma das armas tradicionais mais antigas dos soldados indianos de infantaria é o arco de bambu, barato de fabricar e fácil de transportar e manter. Como o arco também é a arma favorita de muitos caçadores, não faltam arqueiros experientes nos exércitos indianos.",
    16930: "Combinando hastes tensionadas por aço com sistemas mecânicos de tração, esses arcos avançados ampliaram tanto o alcance quanto o poder letal. A inovação marcou a transição de uma defesa baseada em volume para uma implantação em campo focada na precisão, dando aos exércitos profissionais ferramentas dignas de um império.",
    16665: "Embora os arqueiros tenham tradicionalmente feito e mantido seus próprios arcos, os artesãos começaram a fabricar novos tipos de arco, mais fortes e complexos do que antes. Ao adotar esses novos desenhos, podemos transformar esses mestres arqueiros em um recurso militar inestimável.",
    16921: "Esta cultura fez das montanhas mais frias seu lar em uma região que, de outro modo, seria seca e árida. Os pastores e agricultores dessas terras altas são guerreiros resilientes e robustos, e ao longo do tempo o trabalho árduo dessa gente a fez florescer muito acima do deserto árido.",
    16909: "O Sahel é uma planície seca e árida, onde grandes distâncias são frequentemente percorridas a cavalo. Os Cavaleiros do Sahel são guerreiros montados que usam armaduras acolchoadas leves e dardos para devastar seus inimigos. Eles são rápidos e podem enfrentar facilmente desertos e terras áridas.",
    16867: "Feitos de tiras laminadas de chifre, madeira e tendões, nossos arcos compostos são muito menores do que um arco tradicional, embora sejam igualmente poderosos. Esse perfil reduzido torna possível atirar confortavelmente a cavalo, permitindo-nos colocar em campo arqueiros a cavalo verdadeiramente devastadores.",
    16622: "A cavalaria leve é uma força a ser considerada, mas não explora verdadeiramente o potencial do cavalo de guerra. As selas arqueadas nos permitem colocar em campo guerreiros com armaduras pesadas e lanças em riste, o que lhes permite tirar o máximo proveito de seu ímpeto ao investir contra as linhas inimigas.",
    16680: "À medida que nossas terras se expandem, torna-se cada vez mais ineficiente gerenciar diretamente todo o comércio que ocorre em nosso domínio. Ao conceder alvarás de guilda a organizações de mercadores e artesãos, podemos dar-lhes mais autonomia sobre seus negócios e, ao mesmo tempo, garantir que lucremos com seus empreendimentos.",
    16782: "O carvão vegetal é uma parte vital da ferraria, mas precisa de vastas florestas por perto para transformar a madeira nesse combustível essencial. O carvão mineral, aquecido em algo parecido com um forno de carvão vegetal, pode ser transformado em coque. O coque é tão útil quanto o carvão vegetal na ferraria e em outros fogos industriais, sem precisar derrubar uma floresta inteira.",
    16764: "Apesar de todos os avanços na arte da guerra, uma ferramenta antiga ainda é inestimável: o braço. Pequenos recipientes cheios de uma substância inflamável ou explosiva podem ser facilmente fabricados e arremessados por qualquer pessoa com uma técnica de arremesso decente. Distribuir granadas aos soldados antes de uma batalha pode lhes dar uma forma secundária de atacar e abalar o inimigo.",
    16780: "A maneira mais segura de impedir que um navio afunde é evitar, antes de tudo, que seu casco seja rompido. Mas, se isso acontecer, a embarcação ainda poderá ser salva se o casco for dividido em compartimentos com paredes estanques entre eles. Mesmo que o navio seja danificado, um anteparo pode impedir que a água o afunde por completo, salvando homens, suprimentos e o próprio navio no processo.",
    16747: "A melhor porcelana só pode ser produzida em fornos de queima altamente especializados, localizados perto de valiosos depósitos minerais. Esses fornos são construídos em encostas íngremes que ajudam a criar um vórtice de calor para cozer a cerâmica mais dura possível. Embora sejam desafiadores de construir, os produtos que fabricam estão entre as cerâmicas mais elegantes (e mais lucrativas) conhecidas em qualquer corte.",
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
    rows = load_packet_rows()
    records = build_records(rows)
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
    output_path = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_human_packet_v4_decisions.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"decisions={output_path}")
    print(f"review_count={payload['review_count']}")
    print(f"approve_already_ok_count={payload['approve_already_ok_count']}")
    print(f"approve_correction_count={payload['approve_correction_count']}")
    print("apply_ready_now=0")
    print("production_full_recommended_now=false")


if __name__ == "__main__":
    main()
