from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_human_packet_v2_human_decisions"
PACKET_JSONL = Path("reports/20260628_161348_771736_domain_policy_vote_candidate_human_packet.jsonl")
SEGMENT_STATE_RUN_ID = 483
LEDGER_RUN_ID = 76

CORRECTIONS: dict[int, str] = {
    34209: 'providenciarei os serviços de alguns dos guerreiros mais ferozes de nossa fé, para serem usados durante a sua maioridade".',
    38663: "Os naga são criaturas sagradas meio humanas e meio serpentes que residem em Patala, e ocasionalmente podem atravessar para nosso mundo como humanos completos. Nossa família pode traçar sua linhagem até um naga que certa vez visitou a Caxemira e se deitou com um humano.",
    39106: "Construída com gnaisse e calcário com a ajuda de artesãos da Alemanha, esta grande igreja de pedra é uma das primeiras do seu tipo nas terras polonesas e um testemunho do poder da Igreja.\\nAs enormes paredes oferecem aos fiéis a promessa de salvação e também uma proteção mortal crucial. Diz-se que a torre ocidental carrega as marcas das garras do Diabo, ...",
    16631: "Como nossa realeza carrega um fardo maior por sua responsabilidade de governar o reino, ela também deve receber privilégios reservados exclusivamente a ela.",
    6424: "De acordo com a tradição, esta espada envelhecida está ligada ao lendário e bastante popular Dietrich von Bern. Essa conexão conferiu ao item uma fama que, de outra forma, não seria justificada.",
    16616: "Antigos princípios de fortificação ainda são úteis hoje em dia, e podemos usar esse tipo de centro fortificado tanto para reunir tropas quanto para servir como centro administrativo.",
    16853: "O Levante é um lugar desconhecido, com terreno ruim para cargas de cavalaria. Devemos nos adaptar a essas terras e empregar uma cavalaria mais adequada para lutar em nosso novo lar.",
    16694: "Uma melhoria notável em relação aos nossos cadafalsos anteriores, os matacães de pedra têm o mesmo propósito, mas são significativamente mais duráveis e oferecem mais proteção aos nossos defensores.",
    16649: "Apesar de virmos de tribos diferentes, reconhecemos a importância da unidade e da cooperação. A independência é supervalorizada quando a estabilidade é o que leva à segurança e à prosperidade.",
    16572: "O escambo é lento e ineficiente; adotar uma moeda oficialmente reconhecida, como conchas de cauri ou pepitas de metal, irá impulsionar o comércio e aumentar a difusão de ideias em nosso reino.",
    16672: "A invenção do moinho de vento vertical nos permite aproveitar a força do vento com muito mais eficiência do que antes, reduzindo enormemente o trabalho necessário para moer grãos e processar matérias-primas.",
    16927: "Temos uma longa tradição de um exército permanente criado para proteger a capital. Isso nos deu acesso a soldados resistentes que trabalham muito bem com tropas de apoio, como elefantes de guerra.",
    16657: "Para conquistar as torres de pedra inimigas, essas altas torres de madeira com rodas eram empurradas contra elas, permitindo que os soldados alcançassem as muralhas inimigas com alguma segurança.",
    16686: "As terras a nosso leste são pouco povoadas, mas ricas em recursos naturais. Estabelecer uma iniciativa para expandir e fundar propriedades rurais ali levará ao crescimento e à prosperidade de nosso povo.",
    16558: "Formadas por uma dúzia ou mais de camadas de linho costuradas juntas, essas jaquetas podem deter até mesmo flechas pesadas e são uma maneira acessível de equiparmos um regimento inteiro com armadura para a batalha.",
    16865: "Nossos ancestrais viveram nestas florestas por gerações, mas agora elas estão sob a ameaça de estrangeiros. Séculos de experiência acumulada em combates nas florestas nos ajudarão a proteger nossa terra ancestral.",
    16923: "Desde os tempos antigos, o povo de nossas montanhas busca métodos para pastorear seus rebanhos em terrenos inóspitos. Isso os transformou em guerreiros muito resistentes e robustos, chamados abudrar.",
    16704: "A forma definitiva de proteção pessoal, uma armadura de placas bem trabalhada conta com juntas articuladas que garantem uma amplitude de movimento superior à das armaduras anteriores, além de ser praticamente impenetrável às armas inimigas.",
    16877: "Nossa juventude começou a formar clubes que promovem destreza, vigor e comportamento moral. Ao endossar e apoiar esses clubes, vamos garantir um suprimento de soldados aptos que podemos reunir para nossa causa.",
    16570: "Financiar a construção de obras públicas como estradas, escolas e aquedutos permitirá que nossos assentamentos cresçam, garantindo a prosperidade de nosso povo por gerações.",
    16661: "Aqueles habilidosos com o cavalo e a lança sempre serão requisitados, mas nem sempre estão disponíveis. Manter grupos de homens de armas a nosso serviço garantirá que não nos faltem guerreiros experientes quando a guerra inevitavelmente estourar.",
    16714: "Qualquer um pode reivindicar um título, mas, para ser um verdadeiro monarca, é preciso inspirar admiração e majestade nos súditos. Usar os materiais mais raros e exóticos em nossas vestes régias certamente cumprirá esse propósito.",
    16917: "As colinas da savana arborizada têm sido nossa terra natal por gerações. Nossos ancestrais nos mostraram como utilizar este terreno e, com invasores ameaçando nossas fronteiras, podemos usar esse conhecimento em nossa defesa.",
    16702: "Não importa quão fortes sejam as muralhas do nosso inimigo se a terra sob elas puder ser escavada. Empregar sapadores para minar fortificações inimigas é uma maneira eficaz de romper até mesmo as defesas mais resistentes.",
    16724: "Construir diques fortes e confiáveis é uma tarefa essencial se quisermos viver e prosperar nos Países Baixos. Novas técnicas de reforço de diques de terra com esteiras de algas marinhas aumentarão sua resistência e durabilidade.",
    16620: "Um cavalo com o casco partido rapidamente ficará coxo e será incapaz de trabalhar ou de ser conduzido para a batalha. Ao ferrar nossos cavalos com placas de bronze ou ferro, podemos manter seus cascos saudáveis e protegidos de danos.",
    16670: "Criar brasões hereditários para identificar nossas famílias nobres fará com que seus membros se vejam como parte de um todo maior, encorajando-os a melhorar a reputação de sua dinastia, em vez de apenas ficarem obcecados com sua posição pessoal.",
    16873: "Os guerreiros distinguidos desta cultura são designados como garudas e devem lutar até a morte. Espera-se que até mesmo aqueles que seguem um garuda morram se seu líder morrer, seja em batalha ou por sua própria mão.",
    16935: "O salto com vara é uma invenção simples que requer muito treinamento e prática para dominar. Um saltador habilidoso será capaz de se impulsionar sobre grandes vãos e rochas altas, bem como sobre algumas fortificações.",
    16919: "As terras altas onde nossos ancestrais se estabeleceram requerem uma abordagem particular da agricultura. A construção de campos em socalcos nos ensinou a desenvolver novas táticas militares, empregadas por nossos soldados serawit.",
    16597: "Embora muitas das tradições mais antigas dos visigodos tenham sido varridas do mundo há muito tempo, os filhos dos Pireneus se lembram dos costumes antigos e de como a terra era dividida entre filhos e filhas de maneira prática, mas justa.",
    16580: "Os camelos são naturalmente adequados para viver em terrenos desérticos, mas não são inerentemente criaturas de batalha. Nossos criadores e treinadores podem mudar isso, dando-nos acesso a uma cavalaria adaptável que nos permitirá dominar as areias.",
    16678: "Sempre se esperou que nossos súditos prestassem serviço militar, mas há alguns que não podem ou não querem fazê-lo. Se oferecermos a eles uma maneira de cumprir suas obrigações com moeda em vez de aço, teremos a certeza de receber o que nos é devido de todos.",
    16861: "O tradicional círculo de piqueiros é uma formação defensiva poderosa, mas tem ataque e mobilidade limitados. Embora exija grande disciplina para ser empregado, nosso schiltron retilíneo é uma formação superior, capaz de táticas poderosas tanto ofensivas quanto defensivas.",
    16720: "Avanços na ciência, filosofia e lógica transformaram nossas cidades em centros maravilhosos de educação e inovação. Os enormes saltos tecnológicos e culturais que estamos dando agora colocaram ao nosso alcance um potencial até então inimaginável.",
    16726: "Por ser rica e centralmente localizada, a Itália se tornou um importante destino para companhias mercenárias quando não estão em campanha ativa. Como elas já estão aqui, devemos conseguir negociar uma taxa melhor do que normalmente cobrariam.",
    16560: "Estabilidade e prosperidade andam de mãos dadas. Em vez de deixar os vassalos disputarem o poder quando o velho monarca falece, podemos definir um sistema de herança predeterminado e equitativo que eliminará grande parte da instabilidade que tradicionalmente ocorre na sucessão.",
    16618: "À medida que nossas famílias nobres crescem em poder, elas precisam de soldados para fazer cumprir sua vontade. Estabelecer uma ordem de soldados dedicados às suas casas garantirá que tenhamos tropas mais bem treinadas e equipadas do que uma leva comum.",
    16913: "As colinas e montanhas cobertas de florestas de nossa terra natal exigem uma abordagem diferente da guerra. Nossos homens armados, os zbrojnosh, são treinados para usar o terreno a seu favor e provaram ser bons combatentes, capazes de derrotar exércitos muito maiores.",
    16698: "À medida que o tamanho de nossos exércitos aumenta, devemos garantir que cada soldado esteja devidamente armado e protegido por armadura. Estabelecer um arsenal real para produzir e abrigar todo o equipamento de nossos exércitos contribuirá muito para atender a essa necessidade.",
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
        "ledger_run_id": LEDGER_RUN_ID,
        "review_count": len(records),
        "approve_already_ok_count": counts.get("approve_already_ok", 0),
        "approve_correction_count": counts.get("approve_correction", 0),
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "next_action": "controlled_decision_ingest_then_protected_apply_for_corrections",
        "records": records,
    }
    output_path = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_human_packet_v2_decisions.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"decisions={output_path}")
    print(f"review_count={payload['review_count']}")
    print(f"approve_already_ok_count={payload['approve_already_ok_count']}")
    print(f"approve_correction_count={payload['approve_correction_count']}")
    print("apply_ready_now=0")
    print("production_full_recommended_now=false")


if __name__ == "__main__":
    main()
