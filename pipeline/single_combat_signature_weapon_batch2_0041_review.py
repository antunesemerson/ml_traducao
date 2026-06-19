from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


DIAGNOSTIC_JSONL = Path("reports/20260617_164558_196604_single_combat_signature_weapon_composer_diagnostic_run_177.jsonl")
DB_PATH = Path("memory/translation_engine.sqlite")
TARGET_PATH = "single_combat_events_l_spanish.yml"
TARGET_PREFIX = "single_combat.0041"
ALLOWED_DECISIONS = {"corrected", "already_good", "needs_context", "needs_token_delta_review", "blocked"}
BAD_MARKERS = ("\ufffd", "\u00c3\u0192", "\u00c3\u201a", "\u00c3\u00af\u00c2\u00bf\u00c2\u00bd")
TOKEN_RE = re.compile(r"\[[^\]]+\]")
QUESTION_MARK_INSIDE_WORD_RE = re.compile(r"(?<=[A-Za-zÀ-ÿ])\?+(?=[A-Za-zÀ-ÿ])")


def token_counter(text: str | None) -> Counter[str]:
    return Counter(TOKEN_RE.findall(text or ""))


def load_metadata() -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for line in DIAGNOSTIC_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("relative_path") == TARGET_PATH and row.get("source_key_prefix") == TARGET_PREFIX:
            rows[int(row["segment_id"])] = row
    return rows


def load_outputs() -> dict[int, str]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT output.segment_id, output.portuguese_text
        FROM output_segments output
        JOIN source_segments source
          ON source.id = output.segment_id
        WHERE source.relative_path = ?
          AND source.source_key LIKE ?
        """,
        (TARGET_PATH, f"{TARGET_PREFIX}%"),
    ).fetchall()
    return {int(row["segment_id"]): str(row["portuguese_text"] or "") for row in rows}


def make_row(
    metadata: dict[int, dict[str, Any]],
    segment_id: int,
    decision: str,
    *,
    corrected_text: str = "",
    proposed_text_with_token_delta: str = "",
    review_notes: str,
    tokens_preserved: bool,
    token_delta_review_required: bool,
    requires_apply_later: bool,
) -> dict[str, Any]:
    item = metadata[segment_id]
    return {
        "segment_id": segment_id,
        "relative_path": item["relative_path"],
        "source_key": item["source_key"],
        "decision": decision,
        "corrected_text": corrected_text,
        "proposed_text_with_token_delta": proposed_text_with_token_delta,
        "review_notes": review_notes,
        "subpattern": item["subpattern"],
        "tokens_preserved": tokens_preserved,
        "token_delta_review_required": token_delta_review_required,
        "requires_apply_later": requires_apply_later,
    }


def build_rows(metadata: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    corrected: dict[int, tuple[str, str]] = {
        246305: (
            "Com cautela, temendo uma armadilha, avanço por uma brecha óbvia na guarda e desfiro [sc_victor.Custom('SignatureWeaponAttemptedLight')] que [sc_victor.Custom2('SignatureWeaponKillTypeHead2ThirdPersonActiveGendered',SCOPE.sC('sc_loser'))] de modo grotesco.\\n\\n[sc_loser.GetFirstNameNoTooltip] começa imediatamente a engasgar com o jorro de sangue resultante, largando [sc_loser.Custom('signature_weapon')] para levar as mãos ao ferimento.\\n\\nAinda aperta o ferimento quando expira, um minuto inteiro depois.",
            "corrigida naturalidade da cena e regência ao redor de signature_weapon; tokens preservados exatamente",
        ),
        246265: (
            "No meio de uma [sc_loser.Custom('SignatureWeaponAttemptedHeavy')] especialmente extravagante, entro facilmente na guarda de [sc_loser.GetFirstNameNoTooltip] e arranco [sc_loser.Custom('signature_weapon')] da mão com [sc_victor.Custom('SignatureWeaponAttemptedLight')].\\n\\n[sc_loser.GetFirstNameNoTooltip] tenta desesperadamente manter uma aura intimidadora, mas é inútil quando eu ainda estou com [sc_victor.Custom('signature_weapon')].\\n\\nQuando [sc_loser.GetSheHe] se rende, não tenho vergonha de achar isso #EMP incrivelmente#! satisfatório.",
            "corrigido resíduo espanhol increíblemente; tokens preservados exatamente",
        ),
        246274: (
            "Enfrento a investida com um [sc_victor.Custom('SignatureWeaponAttemptedLight')] perfeitamente calculado, que atinge meu oponente no peito, [sc_victor.Custom2('SignatureWeaponKillTypeTorsoThirdPersonPresentParticipleGendered',SCOPE.sC('sc_loser'))] antes de se soltar em um jato de fragmentos de osso e tiras ensanguentadas de carne.\\n\\n[sc_loser.GetFirstNameNoTooltip] gira, mais pelo impulso do que por qualquer outra coisa, rodopiando até virar um amontoado. O choque ainda se mistura à dor em seus espasmos desesperados, cada movimento brusco quase buscando uma saída enquanto seu coração dá os últimos batimentos.",
            "corrigida concordância e fluidez do ataque inicial; tokens preservados exatamente",
        ),
        246295: (
            "\\n\\n[sc_victor.Custom('SignatureWeaponAttemptedHeavy')|U] atravessa diretamente a guarda de [sc_loser.GetFirstNameNoTooltip] até a parte superior do peito. Entre a força, a oportunidade e a concentração quebrada do meu oponente, o golpe é #EMP devastador#!, fazendo voar sangue e ossos por toda parte.\\n\\n[sc_loser.GetFirstNameNoTooltip] desaba, convulsionando, contorcendo-se, uma ruína de [sc_loser.GetWomanMan]. Demora tempo demais até que a morte finalmente venha.",
            "removida literalidade de 'da minha parte' e evitado pronome fixo de gênero; tokens preservados exatamente",
        ),
        246277: (
            "Ousado, mas [sc_victor.GetFaith.WarGodName] recompensa a cautela. Ergo minha [sc_victor.Custom('signature_weapon')] para um [sc_victor.Custom('SignatureWeaponAttemptedLight')] que [sc_victor.Custom2('SignatureWeaponKillTypeHead2ThirdPersonActiveGendered',SCOPE.sC('sc_loser'))].\\n\\nA #EMP explosão#! de sangue e vísceras resultante é recebida com um meio grito, meio gargarejo de pesadelo por [sc_loser.GetFirstNameNoTooltip], que continua cambaleando para a frente.\\n\\nEnquanto tiver [sc_loser.Custom('signature_weapon')] na mão, não posso arriscar um golpe de misericórdia. Em vez disso, [sc_loser.GetFirstNameNoTooltip] e eu apenas nos encaramos até que a maré de sangue derramado apague a luz em seu olhar.",
            "corrigidos resíduos espanhóis explosión/mirada; tokens preservados exatamente",
        ),
        246241: (
            "deixando que [sc_loser.Custom('signature_weapon')] corte o ar sem atrito. Cravo um pé na parte de trás do joelho de [sc_loser.GetFirstNameNoTooltip] e empurro, acertando-lhe a mandíbula quando se vira de forma atrapalhada para me enfrentar.\\n\\nCom [sc_loser.GetFirstNameNoTooltip] cambaleando, basta [sc_loser.Custom('SignatureWeaponAttemptedHeavy')] para fazer a arma voar. Com generosidade, dou à pessoa alguns segundos para se recompor antes de se render.",
            "corrigida literalidade de 'Generosamente' e naturalidade do desarme; tokens preservados exatamente",
        ),
        246219: (
            "[sc_loser.GetFirstNameNoTooltip] luta melhor em terreno aberto e sabe disso; ceder terreno beneficia [sc_loser.GetHerHim]. Isso torna a luta frustrante até que eu veja por perto uma grande rocha de aparência afiada, bem na altura da canela.\\n\\nCom paciência, conduzo meu inimig[sc_loser.Custom('ES_OA')] em direção ao pequeno obstáculo, contando com seu reposicionamento arrogante.",
            "corrigida ordem de frase com GetHerHim e literalidade de justo; tokens preservados exatamente",
        ),
        246302: (
            "Mantenho distância, avançando apenas quando [sc_loser.GetFirstNameNoTooltip] atinge o auge para arrancar pedaços de carne, e assim dançamos por um tempo.\\n\\nNo entanto, por fim, seu frenesi diminui, e um[sc_loser.Custom('ES_XA')] [sc_loser.GetWomanMan], não um[sc_loser.Custom('ES_XA')] berserker, cambaleia diante de mim. A quantidade e a gravidade dos ferimentos fazem com que [sc_loser.GetSheHe] comece a ceder até que…",
            "corrigida concordância e ordem da frase final sem alterar tokens; tokens preservados exatamente",
        ),
        246234: (
            "Arriscando na timidez de [sc_loser.GetFirstNameNoTooltip], lanço-me com [sc_victor.Custom('SignatureWeaponAttemptedHeavy')] capaz de desequilibrar [sc_loser.GetHerHim] totalmente. Sem parar para respirar, acerto [sc_loser.GetHerHim] com força no joelho, e [sc_loser.GetFirstNameNoTooltip] cai com um #EMP baque surdo#!.\\n\\nIsso me dá tempo de sobra para me posicionar para uma morte truculenta. [sc_loser.GetFirstNameNoTooltip] se rende em segundos.",
            "corrigida frase quebrada com GetHerHim e resíduo espanhol 'golpe sordo'; tokens preservados exatamente",
        ),
        246367: (
            "Foi surpreendentemente bem, considerando tudo. [sc_loser.GetFirstNameNoTooltip] e eu ficamos nos encarando de forma estranha: [sc_loser.GetSheHe] não sabe se corre atrás da arma ou se rende, e eu fico pego de surpresa por uma execução tão perfeita.\\n\\nFalando nisso, ponho fim a qualquer ideia que [sc_loser.GetFirstNameNoTooltip] pudesse ter de continuar lutando, erguendo uma sobrancelha e [sc_victor.Custom('SignatureWeaponActionPresentParticiple')] [sc_victor.Custom('signature_weapon')].\\n\\nA rendição é embaraçada, mas rápida.",
            "corrigido fragmento sintático no primeiro parágrafo; tokens preservados exatamente",
        ),
        246386: (
            "O verdadeiro combate é uma arte, e eu sou um[sc_victor.Custom('ES_XA')] artista.\\n\\nSó quando [sc_loser.GetFirstNameNoTooltip] foi deixado quase entorpecido é que eu desfero o golpe final, desarmando [sc_loser.GetHerHim] com [sc_victor.Custom('SignatureWeaponAttemptedHeavy')], que faz o [sc_loser.Custom('signature_weapon')] voar.\\n\\nPreparo-me mental e fisicamente para [sc_victor.Custom2('SignatureWeaponKillTypeThirdPersonFutureGendered',SCOPE.sC('sc_loser'))], pausando perfeitamente no instante em que meu oponente se rende.",
            "corrigido token colado e frase incompleta do desarme; tokens preservados exatamente",
        ),
        246424: (
            "Pouco a pouco o espírito de [sc_loser.GetFirstNameNoTooltip] se quebra. Cada onda de agonia arranca qualquer sensação de habilidade robusta que ele pudesse ter tentado manter. [sc_loser.GetHerHim|U] vira uma massa trêmula que grita e jorra sangue.\\n\\nÉ demais até para mim. Um golpe final com [sc_victor.Custom('SignatureWeaponAttemptedLight')|U] apressa significativamente a morte de [sc_loser.GetFirstNameNoTooltip], mas não o #EMP suficiente#!.",
            "corrigida literalidade da cena e naturalidade do golpe final; tokens preservados exatamente",
        ),
        246384: (
            "[sc_loser.GetFirstNameNoTooltip] cambaleia e se abate com cada golpe, mas cada onda deliciosa de dor que percorre [sc_loser.GetHerHim] apenas me incita a lutar com mais força.\\n\\nQuando ele desaba, está ofegante e cuspindo grossos jorros de sangue.\\n\\nPreparo-me para [sc_victor.Custom2('SignatureWeaponKillTypeRearHeadThirdPersonPresentGendered',SCOPE.sC('sc_loser'))] lentamente. É quase uma decepção ouvir uma rendição atormentada antes que [sc_victor.Custom('signature_weapon')] possa cair.",
            "corrigida concordância e ordem da frase com GetHerHim; tokens preservados exatamente",
        ),
        246380: (
            "abro caminho entre as sarças e as árvores do bosque com selvageria.\\n\\n[sc_loser.GetFirstNameNoTooltip] nem sequer vê o toco podre para o qual conduzo [sc_loser.GetHerHim], tropeçando e caindo para trás com um grito de surpresa, fazendo com que [sc_loser.Custom('signature_weapon')] desapareça pela vegetação rasteira.\\n\\nDepois disso, um chute forte no peito e [sc_victor.Custom('SignatureWeaponAttemptedHeavy')] final são tudo de que preciso para conseguir uma rendição.",
            "corrigida ordem de frase com GetHerHim e naturalidade da abertura; tokens preservados exatamente",
        ),
        246394: (
            "Só depois de me comprometer com a estocada percebo que estou atacando alto demais. Em pânico, tento abaixar a arma, e o espasmo repentino do movimento ultrapassa a guarda de [sc_loser.GetFirstNameNoTooltip] e cai diretamente sobre seu crânio.\\n\\nO golpe [sc_victor.Custom('SignatureWeaponWoundVerb1ThirdPersonPresent')] atravessa diretamente o osso e entra no cérebro, matando [sc_loser.GetHerHim] instantaneamente.\\n\\nPor um momento, o cadáver de [sc_loser.GetFirstNameNoTooltip] cambaleia e vacila, sustentado pelo braço onde minha [sc_victor.Custom('signature_weapon')] está cravada antes de desabar desajeitadamente sobre o [sc_defender.Custom('GroundType')].",
            "corrigido token colado e polida a imagem final; tokens preservados exatamente",
        ),
        246388: (
            "Lentamente, a agonia e a adrenalina sobem em espiral até um crescendo. Solto um rugido estrondoso e avanço em uma onda de [sc_victor.Custom('SignatureWeaponAttemptedHeavyPlural')] que faz [sc_loser.GetFirstNameNoTooltip] ajoelhar-se.\\n\\nMeu oponente tenta aparar, sem perceber que meu joelho segue direto para [sc_loser.Custom('MaskFace')]. Ele cai para trás, ofegante.\\n\\n[sc_loser.GetFirstNameNoTooltip] ainda está ofegante quando se rende segundos depois, os olhos cravados em [sc_victor.Custom('signature_weapon')].",
            "corrigida pontuação e concordância da investida; tokens preservados exatamente",
        ),
        246377: (
            "Finalmente, [sc_loser.GetFirstNameNoTooltip] tropeça de puro cansaço e parto para a ação com [sc_victor.Custom('SignatureWeaponAttemptedHeavy')] capaz de arremessar [sc_loser.GetHerHim] ao chão.\\n\\nMeu oponente tenta se levantar, mas sua luta se transforma em pânico quando eu piso em sua cabeça com minha bota.\\n\\nErgo [sc_victor.Custom('signature_weapon')] para [sc_victor.Custom2('SignatureWeaponKillTypeHeadThirdPersonFutureGendered',SCOPE.sC('sc_loser'))], e consigo uma rendição atordoada em troca dos meus esforços.",
            "corrigida ordem do objeto GetHerHim na frase do arremesso; tokens preservados exatamente",
        ),
        246398: (
            "Vou desgastando gradualmente a postura de [sc_loser.GetFirstNameNoTooltip] até conseguir fazer [sc_loser.GetHerHim] voar com [sc_victor.Custom('SignatureWeaponAttemptedLight')].\\n\\nAssim que meu inimigo está no ar, coloco todo o meu peso em [sc_victor.Custom('SignatureWeaponAttemptedHeavy')] que acerta no instante em que [sc_loser.GetSheHe] toca o chão, [sc_victor.Custom2('SignatureWeaponKillTypeRearTorsoThirdPersonPresentParticipleGendered',SCOPE.sC('sc_loser'))] com um golpe feroz.\\n\\n[sc_loser.GetFirstNameNoTooltip] morre rapidamente.",
            "corrigido token colado e literalidade de 'forma'; tokens preservados exatamente",
        ),
        246369: (
            "Linha por linha, invado seu ego e faço um banquete de suas inseguranças, até que sua guarda se torna descuidada por pura irritação.\\n\\nSó quando o aborrecimento de [sc_loser.GetFirstNameNoTooltip] atinge o auge, eu avanço e arranco [sc_loser.Custom('signature_weapon')] com [sc_victor.Custom('SignatureWeaponAttemptedLight')].\\n\\nUm último comentário sobre sua firmeza sob pressão, e então espero a amarga, amarga rendição. Lágrimas salgadas nunca tiveram gosto tão doce.",
            "corrigida literalidade e tempo verbal sem adicionar tokens; tokens preservados exatamente",
        ),
        246379: (
            "São necessárias algumas tentativas, mas, por fim, consigo empurrar [sc_loser.GetFirstNameNoTooltip] para uma poça de água pantanosa fétida que lhe chega até os joelhos. [sc_loser.GetSheHe|U] escorrega, perdendo [sc_loser.Custom('signature_weapon')] na queda, e afunda sob a superfície com um mergulho asqueroso.\\n\\nEntão é fácil manter uma bota sobre seu [sc_loser.Custom('MaskFace')] a partir de terra firme, e a ameaça de sufocar no pântano fétido me rende uma rendição rápida, embora um tanto gorgolejante.",
            "corrigida concordância, anglicismo 'splash' e preposição de terra firme; tokens preservados exatamente",
        ),
        246403: (
            "O primeiro golpe abre um enorme buraco na guarda de [sc_loser.GetFirstNameNoTooltip], o segundo o priva do uso do braço de [sc_loser.Custom('signature_weapon')]. Meu oponente cambaleia, a lucidez se esvai em meio a uma avalanche de dor.\\n\\nTomo meu tempo para alinhar o próximo golpe e, com [sc_victor.Custom('signature_weapon')], [sc_victor.Custom('SignatureWeaponWoundVerb1PresentParticiple')] profundamente no peito.\\n\\nO próximo ataque simplesmente põe fim à dor.",
            "corrigida regência absurda 'peito de signature_weapon' sem restaurar tokens ausentes; tokens preservados exatamente",
        ),
        246407: (
            "Leva um tempo até que se abra uma brecha adequada na guarda de [sc_loser.GetFirstNameNoTooltip], mas quando isso acontece, é tudo de que preciso. Com [sc_victor.Custom('SignatureWeaponAttemptedHeavy')] de [sc_victor.Custom('signature_weapon')], atravesso a barriga do oponente até o estômago.\\n\\nSangue, bile e os restos da última refeição do meu oponente jorram, encharcando a metade inferior do meu corpo e [sc_loser.GetSheHe] solta um grito ensurdecedor.\\n\\nTalvez eu esteja fazendo um favor ao encerrar rapidamente o sofrimento de [sc_loser.GetFirstNameNoTooltip].",
            "corrigido pronome fixo de gênero e polida a frase do ferimento; tokens preservados exatamente",
        ),
        246392: (
            "Que me amaldiçoem se eu conseguir apontar exatamente #EMP onde#! atingiu [sc_loser.GetFirstNameNoTooltip], mas deve ter acertado em cheio, porque o golpe o faz cambalear para trás. O sangue jorra de algum lugar do peito dele, sangue que [sc_loser.GetSheHe] para e encara com incredulidade.\\n\\nSei como isso termina e me recuso a arriscar que meu oponente me leve junto para o túmulo. [sc_victor.Custom('signature_weapon')|U] ao alto, avanço para desferir o golpe fatal.",
            "corrigido resíduo espanhol dónde e naturalidade da frase final; tokens preservados exatamente",
        ),
    }

    rows: list[dict[str, Any]] = [
        make_row(
            metadata,
            segment_id,
            "corrected",
            corrected_text=text,
            review_notes=notes,
            tokens_preserved=True,
            token_delta_review_required=False,
            requires_apply_later=True,
        )
        for segment_id, (text, notes) in corrected.items()
    ]

    already_good = {
        246266: "texto atual aceitável; helpers de gênero e signature_weapon preservados, sem resíduo espanhol visível",
        246428: "texto atual natural em PT-BR e sem resíduo visível; reopen parece falso neste lote",
        246357: "texto atual aceitável; depende de helper futuro mas não exige correção segura imediata",
    }
    for segment_id, notes in already_good.items():
        rows.append(
            make_row(
                metadata,
                segment_id,
                "already_good",
                review_notes=notes,
                tokens_preserved=True,
                token_delta_review_required=False,
                requires_apply_later=False,
            )
        )

    needs_context = {
        246286: "múltiplos helpers SignatureWeaponKillType/WoundVerb encadeados; correção segura depende de como cada helper renderiza em jogo",
        246308: "SignatureWeaponEndType e SignatureWeaponWoundVerb5 exigem conhecer categoria/renderização antes de reescrever a frase",
        246422: "SignatureWeaponWoundVerb4ThirdPersonPresent parece verbo transitivo; frase segura depende do valor renderizado do helper",
    }
    for segment_id, notes in needs_context.items():
        rows.append(
            make_row(
                metadata,
                segment_id,
                "needs_context",
                review_notes=notes,
                tokens_preserved=True,
                token_delta_review_required=False,
                requires_apply_later=False,
            )
        )

    token_delta = {
        246301: (
            "\\n\\nAvanço contra [sc_loser.GetHerHim] com [sc_victor.Custom('SignatureWeaponAttemptedLightPlural')] em sequência. No começo, parece que meu oponente me repele bem até que começa a ir mais devagar, e sangue jorra de… algum lugar. Acho que devo ter cortado uma artéria.\\n\\nEm um minuto [sc_loser.GetFirstNameNoTooltip] desmaiou. Dou cabo de [sc_loser.GetHerHim].",
            "proposta exigiria trocar GetSheHe por GetHerHim em objetos diretos ausentes no output atual",
        ),
        246298: (
            "Quando [sc_loser.GetSheHe] tropeça, tropeça com força, e estou sobre [sc_loser.GetHerHim] num instante.\\n\\nUm chute rápido na virilha mantém [sc_loser.GetFirstNameNoTooltip] no chão por tempo suficiente para eu [sc_victor.Custom2('SignatureWeaponKillTypeTorso2ThirdPersonSimplePresentGendered',SCOPE.sC('sc_loser'))] com [sc_victor.Custom('signature_weapon')].\\n\\n[sc_loser.GetFirstNameNoTooltip] luta por um tempo, mas eventualmente se recosta e deixa a morte levar [sc_loser.GetHerHim].",
            "proposta exigiria trocar GetSheHe por GetHerHim em objeto de preposição/objeto direto; exige política de token-delta",
        ),
    }
    for segment_id, (proposal, notes) in token_delta.items():
        rows.append(
            make_row(
                metadata,
                segment_id,
                "needs_token_delta_review",
                proposed_text_with_token_delta=proposal,
                review_notes=notes,
                tokens_preserved=False,
                token_delta_review_required=True,
                requires_apply_later=False,
            )
        )

    return sorted(rows, key=lambda row: metadata[int(row["segment_id"])]["source_key"])


def validate(rows: list[dict[str, Any]], outputs: dict[int, str]) -> list[str]:
    errors: list[str] = []
    if len(rows) != 31:
        errors.append(f"expected 31 rows, got {len(rows)}")
    seen = [int(row["segment_id"]) for row in rows]
    if len(set(seen)) != len(seen):
        errors.append("duplicate segment_id")
    for row in rows:
        if not {"segment_id", "source_key", "decision"} <= set(row):
            errors.append(f"missing required fields: {row.get('segment_id')}")
        if row["decision"] not in ALLOWED_DECISIONS:
            errors.append(f"decision not allowed for {row['segment_id']}: {row['decision']}")
        row_text = json.dumps(row, ensure_ascii=False)
        for marker in BAD_MARKERS:
            if marker in row_text:
                errors.append(f"bad encoding marker for {row['segment_id']}")
        if QUESTION_MARK_INSIDE_WORD_RE.search(row_text):
            errors.append(f"question mark inside word for {row['segment_id']}")
        if row["decision"] == "corrected":
            if not row.get("corrected_text"):
                errors.append(f"empty corrected_text for {row['segment_id']}")
            if row.get("token_delta_review_required"):
                errors.append(f"corrected marked token_delta_review_required for {row['segment_id']}")
            if token_counter(row["corrected_text"]) != token_counter(outputs.get(int(row["segment_id"]), "")):
                errors.append(f"token mismatch for corrected {row['segment_id']}")
    return errors


def write_artifacts(rows: list[dict[str, Any]], errors: list[str]) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    reports_dir = Path("reports")
    jsonl_path = reports_dir / f"{stamp}_single_combat_signature_weapon_composer_batch2_0041_decisions_reviewed_chat.jsonl"
    txt_path = reports_dir / f"{stamp}_single_combat_signature_weapon_composer_batch2_0041_decisions_reviewed_chat.txt"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    text = jsonl_path.read_text(encoding="utf-8")
    counts = Counter(row["decision"] for row in rows)
    ids_by_decision = {
        decision: [int(row["segment_id"]) for row in rows if row["decision"] == decision]
        for decision in sorted(ALLOWED_DECISIONS)
    }
    lines = [
        "Single combat signature weapon composer batch 2 0041 review",
        "Scope: relative_path=single_combat_events_l_spanish.yml; source_key_prefix=single_combat.0041",
        "Read-only review: no apply, no confirmations, no source/output writes, no segment-state",
        "",
        f"Total revisado: {len(rows)}",
        "Contagem por decisão:",
    ]
    for decision, count in counts.most_common():
        lines.append(f"- {decision}: {count}")
    lines.extend(
        [
            "",
            f"IDs corrected: {ids_by_decision.get('corrected', [])}",
            f"IDs already_good: {ids_by_decision.get('already_good', [])}",
            f"IDs needs_context: {ids_by_decision.get('needs_context', [])}",
            f"IDs needs_token_delta_review: {ids_by_decision.get('needs_token_delta_review', [])}",
            f"IDs blocked: {ids_by_decision.get('blocked', [])}",
            "",
            "Validação de encoding:",
            f"- bad markers: {any(marker in text for marker in BAD_MARKERS)}",
            f"- question marks inside words: {len(QUESTION_MARK_INSIDE_WORD_RE.findall(text))}",
            "",
            "Validação de tokens:",
            f"- corrected token mismatches/errors: {errors or 'nenhum'}",
            "",
            "Principais padrões corrigidos:",
            "- resíduos espanhóis visíveis: increíblemente, explosión, mirada, sordo, dónde",
            "- tokens colados a palavras visíveis, como fazer[GetHerHim] e matando[GetHerHim]",
            "- ordem de frase quebrada ao redor de GetHerHim/GetSheHe e SignatureWeaponAttempted*",
            "- artigo/preposição e regência ao redor de signature_weapon sem restaurar tokens ausentes",
            "- anglicismo e literalidade em cenas de combate, como splash e justo literal",
            "",
            "Aviso: nenhum apply executado.",
            "",
            f"JSONL: {jsonl_path}",
            f"TXT: {txt_path}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path


def main() -> int:
    metadata = load_metadata()
    outputs = load_outputs()
    rows = build_rows(metadata)
    errors = validate(rows, outputs)
    jsonl_path, txt_path = write_artifacts(rows, errors)
    print(f"jsonl={jsonl_path}")
    print(f"txt={txt_path}")
    print(f"rows={len(rows)}")
    print(f"errors={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
