from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_human_packet_v3_human_decisions"
PACKET_JSONL = Path("reports/20260628_184651_381741_domain_policy_vote_candidate_human_packet.jsonl")
SEGMENT_STATE_RUN_ID = 486

CORRECTIONS: dict[int, str] = {
    46879: "Nos confins do mundo, eles ainda não ouviram falar da verdadeira fé. É nosso dever solene remediar isso.",
    49911: "A contemplação e a adoração da bela arte nos aproximam de Deus e fortalecem nossa fé. É por isso que devo oferecer meu patrocínio a um pintor local para a produção de um novo ícone sagrado!",
    49346: "Bizâncio sempre foi o baluarte mais oriental do cristianismo, mas a guerra atual contra os inimigos da Verdadeira Fé está drenando nossa força e nossos recursos. Embora nossas relações com as outras ramificações da cristandade não sejam fáceis, talvez seja hora de engolir nosso orgulho e pedir ajuda aos nossos irmãos em Cristo.",
    21083: "Você já disse o que tinha a dizer, agora deixe-me dizer o meu...",
    20169: "Aprender as palavras de um vizinho aproxima você um passo da língua do Divino.",
    20131: "Defender o que é seu é de importância primordial para esta cultura.",
    20057: "Às vezes, tudo o que se precisa para deixar tudo melhor é uma boa xícara de chá.",
    20587: "Este personagem foi deserdado pelos costumes de sua cultura e não pode herdar nenhum título.",
    20202: "A ofensiva não é uma boa defesa. Uma boa defesa é ter mais castelos do que qualquer outro lugar no mundo conhecido.",
    19974: "Nesta cultura, qualquer pessoa apta da casa pode pegar em armas por seu suserano quando chamada.",
    20153: "Esta cultura valoriza a modéstia; não se deve ocupar muito espaço nem se considerar melhor do que os outros.",
    20161: "Esta cultura está acostumada a viver em climas secos e sabe onde encontrar água e como trabalhar a terra.",
    20171: "Esta cultura se sente em casa em terrenos amplos e abertos, onde pastoreia grandes grupos de animais.",
    20200: "A menos que você esteja ligado a uma das famílias nobres que ajudaram a forjar o destino desta cultura, você não é nada.",
    20121: "Embora uma armadura de alta qualidade possa salvar uma vida, ter dez armaduras decentes pode vencer uma batalha.",
    20125: "Conflitos fronteiriços são comuns para os governantes desta cultura. As terras frequentemente mudam de mãos de maneira injusta.",
    20188: "Aqueles que estão dispostos a pegar em armas e lutar por sua cultura são dignos de admiração. Não importam as probabilidades.",
    20133: "Nesta cultura, as batalhas não são travadas por prestígio, mas por lucro. Quem se importa com status se você não consegue pagar pela guerra?",
    20220: "O mundo pode ser governado por exércitos, mas esta cultura sabe que ele é verdadeiramente controlado por quem domina o fluxo de ouro pelos mares.",
    20256: "Esta cultura dominou os pântanos e brejos. Embora a vida às vezes seja uma batalha contra o mofo, ela aprendeu a usar a turfa a seu favor.",
    20111: "Esta cultura dá grande importância a ter alguns guerreiros bem treinados. Se você não é o melhor dos melhores, não é bem-vindo para servir.",
    20248: "Esta cultura respeita aqueles cuja carne já provou a lâmina. Seja para provar sua coragem ou criar belos desenhos, eles marcam a própria pele.",
    20210: "Esta cultura está determinada a não abandonar seus costumes de origem e lutará por qualquer um dos seus que seja atacado por outra cultura.",
    20129: "Esta cultura não menospreza aqueles que não podem mais lutar devido a ferimentos; em vez disso, eles são celebrados como heróis e atuam como professores.",
    20254: "Esta cultura sempre esteve cercada por matérias-primas, minério e gemas brutas. Eles têm facilidade em encontrar bons locais para escavações de mineração.",
    20204: "As cidades são o coração urbano pulsante desta cultura, e eles desejam que cada uma de suas metrópoles se torne uma joia invejável conhecida em todo o mundo.",
    20067: "A linhagem é muito importante para esta cultura, a ponto de seus ancestrais terem se tornado figuras míticas e lendárias, com muitos afirmando descender deles.",
    21224: "Acosados continuamente pelo avanço dos invasores, dominar a sela e o arco permite usar pequenos grupos com efeito devastador em incursões e emboscadas de bater e correr.",
    21283: 'Longe do toque dos confortos "civilizados", homens vivem do que conseguem tomar e sonham com a morte nas costas de seus cavalos. Povos mais fracos são empurrados para diante desses guerreiros.',
    21375: "Acredita-se que os ursos sejam manifestações dos deuses; portanto, a sacralidade desses animais deve ser respeitada, e a morte de um deles durante caçadas deve ocorrer com o máximo respeito.",
    20077: "Esta cultura desenvolveu há muito tempo conhecimentos sobre as propriedades medicinais de plantas e árvores; para eles, a maioria das doenças pode ser tratada com o emplastro, a pomada ou o caldo certo.",
    20047: "Esta cultura acumulou minuciosamente conhecimento e experiência na arte e na ciência refinada da criação de cavalos. Sejam destriers ou coursers, os cavalos desse povo são renomados por sua superioridade.",
    20272: "A honra nasce, sim, mas a honra também é conquistada e perdida por meio das ações. Ao buscar e vingar ofensas, ou ao falhar nisso, os soldados se tornam desprezíveis enquanto os jovens se transformam em guerreiros.",
    19980: "Esta cultura faz uso extensivo de eunucos na corte, ocupando posições que vão desde servos domésticos até administradores burocráticos e até mesmo comandantes militares. Homens sem desejo são mais fáceis de receber confiança.",
    21398: "O caminho da pena e o caminho do pincel são elementos importantes para esta cultura. Em vez de depender apenas do poder militar ou de uma administração complexa, os chineses dão ênfase ao aperfeiçoamento de todas as formas de arte.",
    20234: "Desde suas origens nas colinas da Floresta da Boêmia até as escarpas das Montanhas Ore e Tatra, a maioria nesta cultura sabe não apenas como percorrer terras rochosas, mas também como lidar com facilidade com a vida nas montanhas.",
    21198: "Divina e misteriosa são as duas palavras que descrevem a nobreza aos olhos desta cultura. Os governantes que desejam assegurar seu mandato precisam participar de rituais elaborados e manifestar seu poder por meio da exibição de artefatos sagrados.",
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
    output_path = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_human_packet_v3_decisions.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"decisions={output_path}")
    print(f"review_count={payload['review_count']}")
    print(f"approve_already_ok_count={payload['approve_already_ok_count']}")
    print(f"approve_correction_count={payload['approve_correction_count']}")
    print("apply_ready_now=0")
    print("production_full_recommended_now=false")


if __name__ == "__main__":
    main()
