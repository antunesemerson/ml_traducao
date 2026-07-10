from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_human_packet_v7_human_decisions"
PACKET_JSONL = Path("reports/20260628_222729_169699_domain_policy_vote_candidate_human_packet.jsonl")
SEGMENT_STATE_RUN_ID = 492

CORRECTIONS: dict[int, str] = {
    238845: "Uma teologia racionalista que defende que a ética tem uma existência objetiva, derivada das qualidades eternas de Deus, e que os humanos devem, portanto, buscar escolhas éticas.",
    238748: "O Islã é a revelação final da fé abraâmica, guiada pela palavra de Deus, conforme revelada ao profeta Maomé. Ele se concentra em viver uma boa vida em servidão ao único Deus misericordioso.",
    238828: "Longe do centro do mundo árabe, os muçulmanos ibéricos se adaptaram à realidade de governar uma região com grandes populações de cristãos e judeus que tinham tradições estranhas e estrangeiras.",
    237899: "A hospitalidade é uma das maiores virtudes que a humanidade pode demonstrar. Os hóspedes em nossas casas, sejam eles mortais ou divinos, devem ser sempre tratados com a maior honra e respeito.",
    238823: "Uma escola ortodoxa de teologia que sustenta que Alá criou cada momento no tempo, mas que os humanos têm livre-arbítrio para escolher entre o bem, aquilo que Deus ordenou, e o mal, aquilo que Deus proíbe.",
    237336: "Os praticantes do budismo Vinaya mantêm a si mesmos e uns aos outros sob rigorosos padrões morais e éticos, pois a autodisciplina é o alicerce necessário para a busca de uma sabedoria profunda.",
    237932: "Cada um de nós nasceu com um destino, o mais importante dos quais é o dos Táltos: aqueles que são escolhidos desde o nascimento para possuir poderes mágicos e se tornar grandes líderes espirituais.",
    238882: "Os ikhtilafis acreditam na natureza divina dos imames infalíveis, começando por Ali. Deus não apenas os nomeou e inspirou, mas também habita parcialmente neles para guiar seus atos e, por extensão, toda a Umma.",
    240587: "A fé nativa da região escandinava, o paganismo nórdico, gira em torno da reverência aos deuses conhecidos como Æsir. Espera-se que os adeptos vivam, e de preferência morram, de maneira honrosa.",
    240918: "Enfatizando a harmonia e o equilíbrio, os adeptos desta fé nativa mandé reverenciam a prole gêmea de Amma, conhecida em conjunto como Nummo, que era vista como duas partes de um ser completo e perfeito.",
    237831: "Nossas crenças não são tão diferentes das de nossa fé-mãe... Por que discutir sobre detalhes quando concordamos nos pontos principais da doutrina e, o mais importante, na autoridade final para arbitragem?",
    238418: "O mandeísmo traça sua linhagem até Adão, com João Batista como seu maior profeta. O mundo consiste em luz e trevas, e no tempo certo todas as almas mortais serão julgadas quanto a serem dignas de retornar à luz eterna.",
    241514: "Khyarwé Bön, ou \"Bön errante\", é a fé nativa da região tibetana. As crenças exatas variavam de um lugar para outro, mas o ritualismo, as oferendas sacrificiais e as crenças animistas eram comuns entre os fiéis.",
    241519: "Khyarwé Bön, ou \"Bön errante\", é a fé nativa da região tibetana. As crenças exatas variavam de um lugar para outro, mas o ritualismo, as oferendas sacrificiais e as crenças animistas eram comuns entre os fiéis.",
    237613: "Acreditando que qualquer uso de símbolos espirituais viola o Segundo Mandamento de não fazer imagens esculpidas, os iconoclastas procuram destruir a iconografia religiosa existente e rejeitam a criação de novas imagens.",
    241924: "No princípio, havia apenas lama, e foi o primeiro dos kamuy, os espíritos, que fez o mundo como ele é agora; é por meio da intercessão de ape-kamuy que a humanidade pode recorrer aos poderes do mundo espiritual.",
    237754: "Ao nos casarmos com parentes próximos, imitamos as uniões sagradas dos primeiros seres da criação. Boas qualidades serão transmitidas à nossa linhagem familiar e, assim, ganhamos pureza e favor na batalha eterna contra o mal.",
    238449: "O sabeísmo acredita no Deus eternamente existente e em um mundo material criado por Satã. Seus adeptos adoram os guardiões angelicais do mundo, que, junto com os profetas, guiam a humanidade em direção ao paraíso e à reunificação com Deus.",
    240245: "Embora não sigam todas as leis do Profeta, há alguns neste mundo que são mais guiados pela vontade de Alá do que imaginam. Com o tempo, esses semiconvertidos podem até abandonar seus caminhos malignos e se tornar verdadeiros membros da Umma!",
    238539: "Caim foi a primeira vítima do Mal do Demiurgo, criador de toda matéria física. Uma vez que a matéria e os corpos são maus, os corpos devem ser maculados e destruídos pela vida pecaminosa, para que a verdadeira alma humana dentro deles possa ser libertada.",
    238061: "O sacrifício humano, tanto voluntário quanto involuntário, nos aproxima do divino. É costumeiro e virtuoso que alguém se ofereça para se sacrificar em cerimônia, assim como é virtuoso sacrificar aqueles que cometeram crimes imperdoáveis contra os deuses.",
    241731: "A antiga fé nativa basca incorporou há muito tempo outras religiões mais transitórias. Elementos do paganismo celta, do cristianismo, do paganismo helênico e um forte substrato de seus próprios mitos e crenças locais se fundem em uma mistura sincrética.",
    241305: "Os tengristas veem sua existência como sustentada por Tengri, o eterno céu azul, e Eje, a fértil Mãe Terra. Espera-se que os adeptos mantenham o mundo em equilíbrio vivendo uma vida correta e respeitosa; o engano e a subversão são altamente estigmatizados.",
    237598: "Elegendo seus líderes espirituais entre a população em geral, os bogomilos rejeitam todos os aparatos mundanos e instituições de poder, defendendo, em vez disso, um retorno aos métodos anteriores de ensino espiritual praticados por Cristo e seus discípulos.",
    240803: "A veneração da natureza e um profundo respeito pelos ancestrais e suas tradições são o que definem o paganismo fino-úgrico. O \"sisu\" é tido em alta consideração entre os adeptos, um conceito finlandês de determinação estoica, coragem, bravura e resiliência.",
    237258: "A libertação do Samsara não é meramente algo feito individualmente, mas uma experiência compartilhada entre os seres sencientes. Alcançar o estado búdico é um passo no longo caminho para que todos os seres sencientes sejam libertados no Nirvana, e esse é o objetivo virtuoso do Samsara.",
    238063: "A adoração não deve ser feita apenas nos limites de um templo; essas são construções primitivas do homem que o distraem das criações divinas que nos cercam. Nós abraçamos a pedra e o ar livre, organizando as pedras que o Criador nos deu de maneiras que lhe agradam e nos aproximam da divindade.",
    240259: "Juntos, não somos unidos por uma única fé, mas pelas muitas fés que compõem o tecido religioso de nossa sociedade. Embora possamos nos chamar por nomes diferentes e seguir caminhos diferentes, alguns de fora de nosso próprio sistema de crenças aprenderam a conviver em nossa sociedade como se fossem um de nós.",
    240734: "Enfatizando o dever pessoal, a moralidade e a conformidade, o paganismo eslavo é o termo geral para a miríade de crenças nativas de grande parte da Europa Central e Oriental. A comunidade é uma parte importante da maioria delas, e as cerimônias religiosas costumam ser reuniões públicas que celebram a beleza e a alegria.",
    153519: "Ramanuja foi um teólogo indiano do século XI, conhecido por seu papel fundamental no estabelecimento da tradição Sri Vaishnavismo. Defendeu a devoção a Vishnu, promovendo uma abordagem pessoal e compassiva da espiritualidade. Suas interpretações do Vedanta enfatizavam o não dualismo qualificado, um afastamento significativo de outras escolas de pensamento.",
    153505: "William de Ockham foi um filósofo e teólogo inglês do século XIV, célebre pela Navalha de Ockham, o princípio de preferir explicações mais simples. Figura-chave da tradição escolástica, desafiou a autoridade da Igreja e defendeu a separação entre poder secular e religioso. Suas ideias influenciaram o desenvolvimento da lógica, da ciência e do pensamento político.",
    153492: "Basava foi um filósofo indiano e reformador social do século XII que fundou o movimento lingayat. Seus ensinamentos desafiaram o sistema tradicional de castas e enfatizaram a devoção ao Senhor Shiva. Os vachanas de Basava, ou composições poéticas, defendiam a igualdade espiritual e moldaram o movimento Bhakti. Suas ideias impactaram profundamente a sociedade e a religião indianas medievais.",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_packet_rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in PACKET_JSONL.read_text(encoding="utf-8").splitlines() if line.strip()]


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
    output_path = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_human_packet_v7_decisions.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"decisions={output_path}")
    print(f"review_count={payload['review_count']}")
    print(f"approve_already_ok_count={payload['approve_already_ok_count']}")
    print(f"approve_correction_count={payload['approve_correction_count']}")
    print("apply_ready_now=0")
    print("production_full_recommended_now=false")


if __name__ == "__main__":
    main()
