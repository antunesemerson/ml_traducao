from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import db
import local_quality_validator


RULE_VERSION = "issue_select_cstring_object_pronoun_sentence_composer_checkpoint_v1"
POLICY_NAME = "select_cstring_object_pronoun_sentence_composer_shadow_v1"
POLICY_STATUS = "shadow_sentence_composer"
AGENT_KEY = "micro_select_cstring_object_pronoun_sentence_composer"
PRODUCTION_RELEASE_ALLOWED = 0

BLOCKING_VALIDATION_CODES = {
    "spanish_residue",
    "spanish_residue_in_literal",
    "spanish_punctuation",
    "mojibake_or_unexpected_script",
    "utf8_mojibake_sequence",
    "replacement_question_mark_mojibake",
    "token_breakage",
    "placeholder_breakage",
}

SELECT_CSTRING_RE = re.compile(r"Select_CString\((?P<body>.*?)\)", re.IGNORECASE)
STRING_LITERAL_RE = re.compile(r"'([^']*)'")
BRACKETED_TOKEN_RE = re.compile(r"\[[^\]]+\]")

SPANISH_SELECT_LITERAL_MARKERS = {
    "contaste",
    "conto",
    "contó",
    "diste",
    "dio",
    "ensuciaste",
    "ensucio",
    "ensució",
    "eres",
    "es",
    "empezaste",
    "empezó",
    "gobernador",
    "gobernadora",
    "habéis",
    "han",
    "hiciste",
    "hizo",
    "inmovilice",
    "inmovilicé",
    "inmovilizo",
    "inmovilizó",
    "le",
    "les",
    "me",
    "mi persona",
    "olvides",
    "olvide",
    "os",
    "os unisteis",
    "pasaste",
    "paso",
    "pasó",
    "perdiste",
    "perdio",
    "perdió",
    "prefieres",
    "prefiere",
    "puede",
    "puedes",
    "quedaste",
    "quedó",
    "se unieron",
    "te",
    "te lanzas",
    "ti",
    "tu",
    "tus",
    "sus",
    "és",
}

SURFACE_SPANISH_MARKERS = {
    " #emp muy#! ",
    " gobernadora",
    " gobernador",
    " inmovilicé",
    " inmovilizó",
    " empezaste",
    " empezó",
    " quedaste",
    " quedó",
    " hiciste",
    " hizo",
    " perdiste",
    " perdió",
    " prefieres",
    " te lanzas",
    " pasaste",
    " pasó",
    " olvides",
    " olvide",
    " ensuciaste",
    " ensució",
    " contaste",
    " contó",
    " diste",
    " dio ",
    " eres ",
    " puedes ",
    " puede ",
}


Composer = Callable[[str], str]


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def normalize(value: str | None) -> str:
    text = " ".join((value or "").strip().casefold().split())
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def short(value: str | None, limit: int = 220) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def bracket_balance_ok(text: str) -> bool:
    return text.count("[") == text.count("]") and text.count("(") == text.count(")")


def bracketed_tokens(text: str) -> list[str]:
    return BRACKETED_TOKEN_RE.findall(text or "")


def guarded_exact(current: str, expected: str, corrected: str) -> str:
    return corrected if current == expected else ""


def compose_coronation_honored_minorities_ceremony_log(current: str) -> str:
    expected = "[host.GetShortUIName] [Select_CString( host.IsLocalPlayer, 'tuviste', 'tuvo' )] palavras de elogio para os vassalos das minorias que [Select_CString( host.IsLocalPlayer, 'te', 'le' )] juraram lealdade."
    corrected = "[host.GetShortUIName] teve palavras de elogio para os vassalos das minorias que lhe juraram lealdade."
    return guarded_exact(current, expected, corrected)


def compose_wedding_goblet_refusal_log(current: str) -> str:
    expected = "[CHARACTER.GetUIName|U] [Select_CString( CHARACTER.IsLocalPlayer, 'quedaste', 'quedó' )] passou vergonha por se embebedar[Select_CString( CHARACTER.IsLocalPlayer, 'te', 'se' )] e se recusar a ir embora[Select_CString( CHARACTER.IsLocalPlayer, 'te', 'se' )]"
    corrected = "[CHARACTER.GetUIName|U] passou vergonha por se embebedar e se recusar a ir embora"
    return guarded_exact(current, expected, corrected)


def compose_tournament_versus_contest_end_flavor_wrestling_1(current: str) -> str:
    expected = "[ROOT.Char.GetFirstNameOrMeNoTooltip|U] [Select_CString( ROOT.Char.IsLocalPlayer, 'inmovilicé', 'inmovilizó' )] firmemente a [Select_CString( second.IsLocalPlayer, 'mi persona', second.GetFirstNameNoTooltip )] e se [Select_CString( ROOT.Char.IsLocalPlayer, 'me', 'le' )] proclamou vencedor[ROOT.Char.Custom('ES_XA')]."
    corrected = "[ROOT.Char.GetFirstNameOrMeNoTooltip|U] imobilizou firmemente [second.GetFirstNameOrMeNoTooltip] e foi declarad[ROOT.Char.Custom('ES_OA')] vencedor[ROOT.Char.Custom('ES_XA')]."
    return guarded_exact(current, expected, corrected)


def compose_wedding_propermatch_court_log(current: str) -> str:
    expected = "[CHARACTER.GetUIName|U] [CHARACTER.LocalPlayerString( 'empezaste', 'empezó' )] começou a cortejar[Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'te', '' )] a [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'ti', TARGET_CHARACTER.GetUIName )]"
    corrected = "[CHARACTER.GetUIName|U] começou a cortejar [TARGET_CHARACTER.GetUIName]"
    return guarded_exact(current, expected, corrected)


def compose_grant_governorship_interaction_desc(current: str) -> str:
    expected = "Nomear[Select_CString(recipient.IsLocalPlayer, 'te', '' )] a [Select_CString( recipient.IsLocalPlayer, 'ti', recipient.GetShortUIName )] como [Concept( 'governor', Select_CString(recipient.IsFemale, 'gobernadora', 'gobernador' ) )|E] de uma [province|lE]"
    corrected = "Nomear [Select_CString( recipient.IsLocalPlayer, 'você', recipient.GetShortUIName )] como [Concept( 'governor', Select_CString(recipient.IsFemale, 'governadora', 'governador' ) )|E] de uma [province|lE]"
    return guarded_exact(current, expected, corrected)


def compose_support_candidacy_interaction_desc(current: str) -> str:
    expected = "Gastar [influence|lE] para promover[Select_CString( secondary_recipient.IsLocalPlayer, 'te ', ' a ' )][Select_CString( secondary_recipient.IsLocalPlayer, '', secondary_recipient.GetShortUIName )] para se tornar [recipient.GetTopLiege.Custom('GetGovernorConcept')|l] ou [secondary_recipient.GetTopLiege.GetTitleAsNameNoTooltip|lV]"
    corrected = "Gastar [influence|lE] para promover [Select_CString( secondary_recipient.IsLocalPlayer, 'você', secondary_recipient.GetShortUIName )] a [recipient.GetTopLiege.Custom('GetGovernorConcept')|l] ou [secondary_recipient.GetTopLiege.GetTitleAsNameNoTooltip|lV]"
    return guarded_exact(current, expected, corrected)


def compose_japanese_korean_child_learned_chinese_effect(current: str) -> str:
    expected = "As novas habilidades linguísticas de[Select_CString( child_learned_chinese.IsLocalPlayer, 'mi personaje', child_learned_chinese.GetFirstName)] [Select_CString( child_learned_chinese.IsLocalPlayer, 'me', 'le' )] os prepararão para uma vida na corte."
    corrected = "As novas habilidades linguísticas de [child_learned_chinese.GetFirstName] prepararão [child_learned_chinese.GetFirstName] para uma vida na corte."
    return guarded_exact(current, expected, corrected)


def compose_hunt_murder_accomplice_tt(current: str) -> str:
    expected = "[THIS.Char.GetShortUIName|U] [Select_CString(THIS.Char.IsLocalPlayer, 'intentarás', 'intentará')] assassinato[Select_CString( murder_target.IsLocalPlayer, 'te', '' )] de [Select_CString( murder_target.IsLocalPlayer, 'ti', murder_target.GetShortUIName )]"
    corrected = "[THIS.Char.GetShortUIName|U] tentará assassinar [Select_CString( murder_target.IsLocalPlayer, 'você', murder_target.GetShortUIName )]"
    return guarded_exact(current, expected, corrected)


def compose_hunt_abduct_accomplice_tt(current: str) -> str:
    expected = "[THIS.Char.GetShortUIName|U] [Select_CString(THIS.Char.IsLocalPlayer, 'intentarás', 'intentará')] sequestrar[Select_CString( abduct_target.IsLocalPlayer, 'te', '' )] a [Select_CString( abduct_target.IsLocalPlayer, 'ti', abduct_target.GetShortUIName )]"
    corrected = "[THIS.Char.GetShortUIName|U] tentará sequestrar [Select_CString( abduct_target.IsLocalPlayer, 'você', abduct_target.GetShortUIName )]"
    return guarded_exact(current, expected, corrected)


def compose_friend_good_food(current: str) -> str:
    expected = "[CHARACTER.GetShortUIName|U] [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'te', 'le' )] [Select_CString( CHARACTER.IsLocalPlayer, 'diste', 'dio' )] uma ótima refeição para [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'ti', TARGET_CHARACTER.GetShortUIName )]"
    corrected = "[CHARACTER.GetShortUIName|U] deu uma ótima refeição para [TARGET_CHARACTER.GetShortUIName]"
    return guarded_exact(current, expected, corrected)


def compose_friend_good_food_corresponding(current: str) -> str:
    expected = "[TARGET_CHARACTER.GetShortUIName|U] [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le' )] [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'diste', 'dio' )] uma grande refeição para [Select_CString( CHARACTER.IsLocalPlayer, 'ti', CHARACTER.GetShortUIName )]"
    corrected = "[TARGET_CHARACTER.GetShortUIName|U] deu uma grande refeição para [CHARACTER.GetShortUIName]"
    return guarded_exact(current, expected, corrected)


def compose_friend_shared_meal(current: str) -> str:
    expected = "[CHARACTER.GetShortUIName|U] [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'te', 'le' )] [Select_CString( CHARACTER.IsLocalPlayer, 'diste', 'dio' )] uma refeição especial para [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'ti', TARGET_CHARACTER.GetShortUIName )]"
    corrected = "[CHARACTER.GetShortUIName|U] deu uma refeição especial para [TARGET_CHARACTER.GetShortUIName]"
    return guarded_exact(current, expected, corrected)


def compose_friend_shared_meal_corresponding(current: str) -> str:
    expected = "[TARGET_CHARACTER.GetShortUIName|U] [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le' )] [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'diste', 'dio' )] deu uma refeição especial a [Select_CString( CHARACTER.IsLocalPlayer, 'ti', CHARACTER.GetShortUIName )]"
    corrected = "[TARGET_CHARACTER.GetShortUIName|U] deu uma refeição especial a [CHARACTER.GetShortUIName]"
    return guarded_exact(current, expected, corrected)


def compose_friend_gift(current: str) -> str:
    expected = "[CHARACTER.GetShortUIName|U] [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'te', 'le' )] [Select_CString( CHARACTER.IsLocalPlayer, 'diste', 'dio' )] a [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'ti', TARGET_CHARACTER.GetShortUIName )] um presente maravilhoso"
    corrected = "[CHARACTER.GetShortUIName|U] deu um presente maravilhoso a [TARGET_CHARACTER.GetShortUIName]"
    return guarded_exact(current, expected, corrected)


def compose_friend_told_me_scary_stories(current: str) -> str:
    expected = "[TARGET_CHARACTER.GetShortUIName|U] [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'le', 'te' )] [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'contaste', 'contó' )] contou histórias assustadoras para [Select_CString( CHARACTER.IsLocalPlayer, 'ti', CHARACTER.GetShortUIName )]"
    corrected = "[TARGET_CHARACTER.GetShortUIName|U] contou histórias assustadoras para [CHARACTER.GetShortUIName]"
    return guarded_exact(current, expected, corrected)


def compose_friend_told_me_scary_stories_corresponding(current: str) -> str:
    expected = "[CHARACTER.GetShortUIName|U] [Select_CString( CHARACTER.IsLocalPlayer, 'le', 'te' )] [Select_CString( CHARACTER.IsLocalPlayer, 'contaste', 'contó' )] uma história de terror para [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'ti', TARGET_CHARACTER.GetShortUIName )]"
    corrected = "[CHARACTER.GetShortUIName|U] contou uma história de terror para [TARGET_CHARACTER.GetShortUIName]"
    return guarded_exact(current, expected, corrected)


def compose_hunt_lover_secret_join_log(current: str) -> str:
    expected = "Alguém se juntou a [Select_CString( CHARACTER.IsLocalPlayer, 'você', CHARACTER.GetShortUIName )] e a [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'você', TARGET_CHARACTER.GetShortUIName )] quando [Select_CString(Or(CHARACTER.IsLocalPlayer, TARGET_CHARACTER.IsLocalPlayer), 'os', 'les')] flagrou em flagrante"
    corrected = "Alguém se juntou a [Select_CString( CHARACTER.IsLocalPlayer, 'você', CHARACTER.GetShortUIName )] e a [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'você', TARGET_CHARACTER.GetShortUIName )] ao flagrar ambos em flagrante"
    return guarded_exact(current, expected, corrected)


def compose_vizierate_0001_reason_friend_foiled_conspiracy_together(current: str) -> str:
    expected = "[TARGET_CHARACTER.GetShortUIName|U] e [CHARACTER.GetShortUIName] [Select_CString( CHARACTER.IsLocalPlayer, 'os unisteis', 'se unieron' )] brevemente para frustrar uma conspiração que [Select_CString( CHARACTER.IsLocalPlayer, 'os', 'les' )] ameaçava ambos, e [Select_CString( CHARACTER.IsLocalPlayer, 'habéis', 'han' )] permanecido em contato próximo desde então."
    corrected = "[TARGET_CHARACTER.GetShortUIName|U] e [CHARACTER.GetShortUIName] se uniram brevemente para frustrar uma conspiração que ameaçava ambos, e permaneceram em contato próximo desde então."
    return guarded_exact(current, expected, corrected)


def compose_vizierate_0001_reason_rival_party_foes(current: str) -> str:
    expected = "A [Select_CString( CHARACTER.IsLocalPlayer, 'você', CHARACTER.GetShortUIName )] e a [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'você', TARGET_CHARACTER.GetShortUIName )] [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'os', 'les' )] continuam sendo convidados para os mesmos banquetes, apesar de um ódio nascente (e de contribuir para ele)."
    corrected = "[CHARACTER.GetShortUIName|U] e [TARGET_CHARACTER.GetShortUIName] continuam sendo convidados para os mesmos banquetes, apesar de um ódio nascente (e de contribuir para ele)."
    return guarded_exact(current, expected, corrected)


def compose_is_in_scapegoating_diarchy_visibility_trigger_tt_explanation(current: str) -> str:
    expected = "O [scales_of_power|lE] [Select_CString(actor.IsLocalPlayer, 'te', '')] favorece a [Select_CString(actor.IsLocalPlayer,'ti', actor.GetShortUIName)] com força demais[SelectLocalization( actor.IsLocalPlayer, 'is_in_scapegoating_diarchy_visibility_trigger.tt.explanation.liege', '' )]"
    corrected = "O [scales_of_power|lE] favorece [Select_CString(actor.IsLocalPlayer, 'você', actor.GetShortUIName)] com força demais[SelectLocalization( actor.IsLocalPlayer, 'is_in_scapegoating_diarchy_visibility_trigger.tt.explanation.liege', '' )]"
    return guarded_exact(current, expected, corrected)


def compose_is_allowed_to_revoke_vassal_desc(current: str) -> str:
    expected = "[recipient.GetShortUIName|U] [Select_CString( recipient.IsLocalPlayer, 'eres', 'es' )] um[recipient.Custom('ES_XA')] [criminal|lE] conhecid[recipient.Custom('ES_OA')] #weak ([recipient.GetRevokeReasons( actor.Self )])#!, o que [Select_CString( actor.IsLocalPlayer, 'te', 'le' )] permite revogar os [Select_CString( recipient.IsLocalPlayer, 'tus', 'sus' )] [vassals|lE] sem que ninguém [Select_CString( actor.IsLocalPlayer, 'te', 'le' )] possa considerar [Concept('tyrant',Select_CString(actor.IsFemale,'tirana', 'tirano'))|E]"
    corrected = "[recipient.GetShortUIName|U] é um[recipient.Custom('ES_XA')] [criminal|lE] conhecid[recipient.Custom('ES_OA')] #weak ([recipient.GetRevokeReasons( actor.Self )])#!, permitindo que [Select_CString( actor.IsLocalPlayer, 'você', actor.GetShortUIName )] revogue os [Select_CString( recipient.IsLocalPlayer, 'seus', 'seus' )] [vassals|lE] sem ser vist[actor.Custom('ES_OA')] como [Concept('tyrant',Select_CString(actor.IsFemale,'tirana', 'tirano'))|E]"
    return guarded_exact(current, expected, corrected)


def compose_is_allowed_to_revoke_admin_government(current: str) -> str:
    expected = "Como [TARGET_CHARACTER.GetShortUIName] [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'eres', 'es' )] [Concept( 'governor', Select_CString(TARGET_CHARACTER.IsFemale, 'gobernadora', 'gobernador' ))|E], [CHARACTER.GetShortUIName] [Select_CString( CHARACTER.IsLocalPlayer, 'puedes', 'puede' )] revogar [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'tus', 'sus' )] títulos sem que ninguém possa considerar[Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le' )] um[CHARACTER.Custom('ES_XA')] [Concept('tyrant',Select_CString(CHARACTER.IsFemale,'tirana', 'tirano'))|E]"
    corrected = "Como [TARGET_CHARACTER.GetShortUIName] é [Concept( 'governor', Select_CString(TARGET_CHARACTER.IsFemale, 'governadora', 'governador' ))|E], [Select_CString( CHARACTER.IsLocalPlayer, 'você', CHARACTER.GetShortUIName )] pode revogar os títulos de [TARGET_CHARACTER.GetShortUIName] sem ser vist[CHARACTER.Custom('ES_OA')] como [Concept('tyrant',Select_CString(CHARACTER.IsFemale,'tirana', 'tirano'))|E]"
    return guarded_exact(current, expected, corrected)


def compose_nick_the_bald_ironic_desc(current: str) -> str:
    expected = "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] [Select_CString( CHARACTER.IsLocalPlayer, 'pasaste', 'pasó' )] grande parte da vida sem uma coroa que [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le' )] adornasse a cabeça, e a corte se nega a deixar que o [Select_CString( CHARACTER.IsLocalPlayer, 'olvides', 'olvide' )]."
    corrected = "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] passou grande parte da vida sem uma coroa adequada a adornar sua cabeça, e a corte se recusa a deixar que isso caia no esquecimento."
    return guarded_exact(current, expected, corrected)


def compose_nick_the_judge_desc(current: str) -> str:
    expected = "$k_israel$ tem clamado por juízes justos e verdadeiros ao longo da história. Às vezes se [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le' )] lembra entre eles."
    corrected = "$k_israel$ tem clamado por juízes justos e verdadeiros ao longo da história. [CHARACTER.GetShortUINameNoTooltipNoFormat|U] às vezes é lembrad[CHARACTER.Custom('ES_OA')] entre eles."
    return guarded_exact(current, expected, corrected)


def compose_nick_the_jade_unicorn_desc(current: str) -> str:
    expected = "A [Select_CString( CHARACTER.IsLocalPlayer, 'ti', CHARACTER.GetShortUINameNoTooltipNoFormat )] se [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le' )] é considerado(a) imbatível com um bastão."
    corrected = "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] é considerad[CHARACTER.Custom('ES_OA')] imbatível com um bastão."
    return guarded_exact(current, expected, corrected)


def compose_nick_the_turbulent_river_dragon_desc(current: str) -> str:
    expected = "A [Select_CString( CHARACTER.IsLocalPlayer, 'ti', CHARACTER.GetShortUINameNoTooltipNoFormat )] [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le' )] gostam de água e pântanos como um… dragão."
    corrected = "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] gosta de água e pântanos como um… dragão."
    return guarded_exact(current, expected, corrected)


def compose_nick_the_diplomat_desc(current: str) -> str:
    expected = "Uma solução pacífica é aquela que pode ser alcançada com palavras e a [Select_CString( CHARACTER.IsLocalPlayer, 'ti', CHARACTER.GetSheHe)] se [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le')] dão #EMP muy#! muito bem com as palavras."
    corrected = "Uma solução pacífica é aquela que pode ser alcançada com palavras, e [CHARACTER.GetShortUINameNoTooltipNoFormat] é #EMP excelente#! com as palavras."
    return guarded_exact(current, expected, corrected)


def compose_nick_the_dung_named_desc(current: str) -> str:
    expected = "[Select_CString( CHARACTER.IsLocalPlayer, 'Tus', 'Sus' )] detratores sussurram que [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'se' )] [Select_CString( CHARACTER.IsLocalPlayer, 'ensuciaste', 'ensució' )] na pia batismal durante o batismo…"
    corrected = "[CHARACTER.GetShortUINamePossessiveNoTooltipNoFormat|U] detratores sussurram que [CHARACTER.GetSheHe] sujou a pia batismal durante o batismo…"
    return guarded_exact(current, expected, corrected)


def compose_nick_the_toxophilite_desc(current: str) -> str:
    expected = "Arcos, projéteis, balistas… Poucas coisas na arte do combate direto à distância que não [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le')] entusiasmem."
    corrected = "Arcos, projéteis, balistas… Há poucas coisas na arte do combate direto à distância que não entusiasmem [CHARACTER.GetShortUINameNoTooltipNoFormat]."
    return guarded_exact(current, expected, corrected)


def compose_nick_the_stallion_desc(current: str) -> str:
    expected = "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] [Select_CString( CHARACTER.IsLocalPlayer, 'eres', 'es' )] forte, atlétic[CHARACTER.Custom('ES_OA')]… e muitas vezes se [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le' )] olha com olhos de desejo."
    corrected = "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] é forte, atlétic[CHARACTER.Custom('ES_OA')]… e muitas vezes é olhad[CHARACTER.Custom('ES_OA')] com desejo."
    return guarded_exact(current, expected, corrected)


def compose_nick_the_jolly_desc(current: str) -> str:
    expected = "Música e boa comida! Isso é tudo que a [Select_CString( CHARACTER.IsLocalPlayer, 'ti', CHARACTER.GetShortUINameNoTooltipNoFormat )] [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le' )] importa aos olhos de [Select_CString( CHARACTER.IsLocalPlayer, 'tus', 'sus' )] vassalos."
    corrected = "Música e boa comida! Isso é tudo que importa para [Select_CString( CHARACTER.IsLocalPlayer, 'você', CHARACTER.GetShortUINameNoTooltipNoFormat )] aos olhos de [Select_CString( CHARACTER.IsLocalPlayer, 'seus', 'seus' )] vassalos."
    return guarded_exact(current, expected, corrected)


def compose_nick_tanglehair_desc(current: str) -> str:
    expected = "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] [Select_CString( CHARACTER.IsLocalPlayer, 'hiciste', 'hizo' )] fez um voto: não pentear, cortar ou limpar[Select_CString( CHARACTER.IsLocalPlayer, 'te', 'se' )] o cabelo antes de unir os senhores menores em conflito da Noruega."
    corrected = "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] fez um voto: não pentear, cortar ou limpar o cabelo antes de unir os senhores menores em conflito da Noruega."
    return guarded_exact(current, expected, corrected)


def compose_nick_hildetand_desc(current: str) -> str:
    expected = "Abundam as lendas sobre como [CHARACTER.GetShortUINameNoTooltipNoFormat] [Select_CString( CHARACTER.IsLocalPlayer, 'perdiste', 'perdió' )] uns dentes de forma dramática na batalha, apenas para que milagrosamente [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le' )] crescessem dois novos."
    corrected = "Abundam as lendas sobre como [CHARACTER.GetShortUINameNoTooltipNoFormat] perdeu alguns dentes de forma dramática na batalha, apenas para que milagrosamente lhe nascessem dois novos."
    return guarded_exact(current, expected, corrected)


def compose_nick_the_anointed_desc(current: str) -> str:
    expected = "Os camponeses sussurram que [CHARACTER.GetFaith.HighGodName] [Select_CString( CHARACTER.IsLocalPlayer, ' te', '' )] escolheu [Select_CString( CHARACTER.IsLocalPlayer, 'ti', CHARACTER.GetShortUINameNoTooltipNoFormat )] para realizar Sua obra no mundo mortal."
    corrected = "Os camponeses sussurram que [CHARACTER.GetFaith.HighGodName] escolheu [Select_CString( CHARACTER.IsLocalPlayer, 'você', CHARACTER.GetShortUINameNoTooltipNoFormat )] para realizar Sua obra no mundo mortal."
    return guarded_exact(current, expected, corrected)


def compose_nick_the_victorious_desc(current: str) -> str:
    expected = "O triunfo [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le' )] chega naturalmente a [Select_CString( CHARACTER.IsLocalPlayer, 'ti', CHARACTER.GetShortUINameNoTooltipNoFormat )] e o mundo concorda."
    corrected = "O triunfo chega naturalmente a [CHARACTER.GetShortUINameNoTooltipNoFormat], e o mundo concorda."
    return guarded_exact(current, expected, corrected)


def compose_nick_the_hotspur_desc(current: str) -> str:
    expected = "Dizem que [CHARACTER.GetShortUINameNoTooltipNoFormat] [Select_CString( CHARACTER.IsLocalPlayer, 'eres', 'es' )] tão corajoso nas [Select_CString( CHARACTER.IsLocalPlayer, 'tus', 'sus')] decisões que [Select_CString( CHARACTER.IsLocalPlayer, 'te lanzas', 'se lanza')] a elas sem sequer tirar[Select_CString( CHARACTER.IsLocalPlayer, 'te', 'se')] as esporas."
    corrected = "Dizem que [CHARACTER.GetShortUINameNoTooltipNoFormat] é tão audaz nas decisões que se lança a elas sem sequer tirar as esporas."
    return guarded_exact(current, expected, corrected)


def compose_nick_the_stutterer_desc(current: str) -> str:
    expected = "As pessoas se deliciam em lembrar[Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le' )] algumas poucas palavras murmurejadas."
    corrected = "As pessoas se deliciam em lembrar [CHARACTER.GetShortUINameNoTooltipNoFormat] de algumas poucas palavras murmurejadas."
    return guarded_exact(current, expected, corrected)


def compose_nick_the_hermit_desc(current: str) -> str:
    expected = "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] [Select_CString( CHARACTER.IsLocalPlayer, 'prefieres', 'prefiere' )] não gosta de companhia e raramente é [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le' )] visto."
    corrected = "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] não gosta de companhia e raramente é visto."
    return guarded_exact(current, expected, corrected)


def compose_nick_the_venerable_desc(current: str) -> str:
    expected = "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] [Select_CString( CHARACTER.IsLocalPlayer, 'has', 'ha' )] passou a vida de maneira respeitável, o que [Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le')] lhe rendeu confiança."
    corrected = "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] passou a vida de maneira respeitável, o que lhe rendeu confiança."
    return guarded_exact(current, expected, corrected)


def compose_nick_shinno_desc(current: str) -> str:
    expected = "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] [Select_CString( CHARACTER.IsLocalPlayer, 'és', 'é' )] célebre por ter-[Select_CString( CHARACTER.IsLocalPlayer, 'te', 'se' )] proclamado \\\"[Select_CString( CHARACTER.IsFemale, 'a nova imperatriz', 'o novo imperador' )]\\\" do Japão, e liderar uma rebelião contra o governo central."
    corrected = "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] é célebre por ter se proclamado \\\"[Select_CString( CHARACTER.IsFemale, 'a nova imperatriz', 'o novo imperador' )]\\\" do Japão, e liderar uma rebelião contra o governo central."
    return guarded_exact(current, expected, corrected)


COMPOSERS: dict[str, Composer] = {
    "coronation_honored_minorities_ceremony_log": compose_coronation_honored_minorities_ceremony_log,
    "wedding_goblet_refusal_log": compose_wedding_goblet_refusal_log,
    "tournament_versus_contest_end_flavor_wrestling_1": compose_tournament_versus_contest_end_flavor_wrestling_1,
    "wedding_propermatch_court_log": compose_wedding_propermatch_court_log,
    "grant_governorship_interaction_desc": compose_grant_governorship_interaction_desc,
    "support_candidacy_interaction_desc": compose_support_candidacy_interaction_desc,
    "japanese_korean_child_learned_chinese_effect": compose_japanese_korean_child_learned_chinese_effect,
    "hunt_murder_accomplice_tt": compose_hunt_murder_accomplice_tt,
    "hunt_abduct_accomplice_tt": compose_hunt_abduct_accomplice_tt,
    "friend_good_food": compose_friend_good_food,
    "friend_good_food_corresponding": compose_friend_good_food_corresponding,
    "friend_shared_meal": compose_friend_shared_meal,
    "friend_shared_meal_corresponding": compose_friend_shared_meal_corresponding,
    "friend_gift": compose_friend_gift,
    "friend_told_me_scary_stories": compose_friend_told_me_scary_stories,
    "friend_told_me_scary_stories_corresponding": compose_friend_told_me_scary_stories_corresponding,
    "hunt_lover_secret_join_log": compose_hunt_lover_secret_join_log,
    "vizierate_0001.reason.friend.foiled_conspiracy_together": compose_vizierate_0001_reason_friend_foiled_conspiracy_together,
    "vizierate_0001.reason.rival.party_foes": compose_vizierate_0001_reason_rival_party_foes,
    "is_in_scapegoating_diarchy_visibility_trigger.tt.explanation": compose_is_in_scapegoating_diarchy_visibility_trigger_tt_explanation,
    "IS_ALLOWED_TO_REVOKE_VASSAL_DESC": compose_is_allowed_to_revoke_vassal_desc,
    "IS_ALLOWED_TO_REVOKE_ADMIN_GOVERNMENT": compose_is_allowed_to_revoke_admin_government,
    "nick_the_bald_ironic_desc": compose_nick_the_bald_ironic_desc,
    "nick_the_judge_desc": compose_nick_the_judge_desc,
    "nick_the_jade_unicorn_desc": compose_nick_the_jade_unicorn_desc,
    "nick_the_turbulent_river_dragon_desc": compose_nick_the_turbulent_river_dragon_desc,
    "nick_the_diplomat_desc": compose_nick_the_diplomat_desc,
    "nick_the_dung_named_desc": compose_nick_the_dung_named_desc,
    "nick_the_toxophilite_desc": compose_nick_the_toxophilite_desc,
    "nick_the_stallion_desc": compose_nick_the_stallion_desc,
    "nick_the_jolly_desc": compose_nick_the_jolly_desc,
    "nick_tanglehair_desc": compose_nick_tanglehair_desc,
    "nick_hildetand_desc": compose_nick_hildetand_desc,
    "nick_the_anointed_desc": compose_nick_the_anointed_desc,
    "nick_the_victorious_desc": compose_nick_the_victorious_desc,
    "nick_the_hotspur_desc": compose_nick_the_hotspur_desc,
    "nick_the_stutterer_desc": compose_nick_the_stutterer_desc,
    "nick_the_hermit_desc": compose_nick_the_hermit_desc,
    "nick_the_venerable_desc": compose_nick_the_venerable_desc,
    "nick_shinno_desc": compose_nick_shinno_desc,
}


def latest_context_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_context_microagent_diagnostic_runs
        WHERE candidate_observations > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise SystemExit("No context microagent diagnostic run with observations found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_object_pronoun_sentence_composer_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_context_run_id INTEGER NOT NULL,
            source_queue_run_id INTEGER NOT NULL,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            candidate_segments INTEGER NOT NULL DEFAULT 0,
            composer_rules_available INTEGER NOT NULL DEFAULT 0,
            composed_count INTEGER NOT NULL DEFAULT 0,
            segment_closure_candidates INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            block_counts_json TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_object_pronoun_sentence_composer_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_context_run_id INTEGER NOT NULL,
            source_context_item_ids_json TEXT,
            source_queue_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            sentence_rule TEXT NOT NULL,
            composed INTEGER NOT NULL DEFAULT 0,
            segment_closure_candidate INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            token_delta_kind TEXT,
            current_text TEXT,
            corrected_text TEXT,
            validation_issues_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def fetch_segments(conn, context_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.id AS source_context_item_id,
            item.run_id AS source_context_run_id,
            item.source_queue_run_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.current_text,
            item.english_text,
            item.spanish_text
        FROM ml_issue_select_cstring_context_microagent_diagnostic_items item
        WHERE item.run_id = ?
          AND item.context_category = 'object_pronoun_context'
        ORDER BY item.relative_path, item.source_key, item.select_index, item.segment_id
        """,
        (context_run_id,),
    ).fetchall()

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["segment_id"])].append(dict(row))

    segments: list[dict[str, Any]] = []
    for segment_rows in grouped.values():
        first = segment_rows[0]
        segments.append(
            {
                **first,
                "source_context_item_ids": [int(row["source_context_item_id"]) for row in segment_rows],
            }
        )
    segments.sort(key=lambda row: (row["relative_path"], row["source_key"], row["segment_id"]))
    return segments


def blocking_validation_issues(text: str | None) -> list[dict[str, Any]]:
    validation = local_quality_validator.validate_text(text)
    issues = validation.get("issues") or []
    return [
        issue
        for issue in issues
        if issue.get("severity") == "high" or issue.get("code") in BLOCKING_VALIDATION_CODES
    ]


def residual_select_cstring_literals(text: str | None) -> list[str]:
    hits: list[str] = []
    for match in SELECT_CSTRING_RE.finditer(text or ""):
        body = match.group("body")
        for literal_match in STRING_LITERAL_RE.finditer(body):
            literal = literal_match.group(1)
            if normalize(literal) in {normalize(marker) for marker in SPANISH_SELECT_LITERAL_MARKERS}:
                hits.append(literal)
    return hits


def residual_surface_markers(text: str | None) -> list[str]:
    normalized_text = normalize(text)
    hits: list[str] = []
    for marker in SURFACE_SPANISH_MARKERS:
        normalized_marker = normalize(marker)
        if not normalized_marker:
            continue
        if not any(char.isalpha() for char in normalized_marker):
            if normalized_marker in normalized_text:
                hits.append(marker.strip())
            continue
        pattern = rf"(?<![a-z]){re.escape(normalized_marker)}(?![a-z])"
        if re.search(pattern, normalized_text):
            hits.append(marker.strip())
    return hits


def token_delta_kind(current: str, corrected: str) -> str:
    before = bracketed_tokens(current)
    after = bracketed_tokens(corrected)
    if before == after:
        return "unchanged"
    before_set = set(before)
    after_set = set(after)
    if after_set.issubset(before_set):
        return "reduced_or_simplified"
    if before_set.issubset(after_set):
        return "expanded"
    return "changed"


def classify(row: dict[str, Any]) -> dict[str, Any]:
    current_text = row.get("current_text") or ""
    source_key = row.get("source_key") or ""
    composer = COMPOSERS.get(source_key)
    block_reasons: list[str] = []
    validation_issues: list[dict[str, Any]] = []
    corrected_text = ""

    if not composer:
        block_reasons.append("no_sentence_composer_rule")
    else:
        corrected_text = composer(current_text)
        if not corrected_text:
            block_reasons.append("exact_source_text_mismatch")

    if corrected_text:
        if corrected_text == current_text:
            block_reasons.append("no_text_delta")
        if not bracket_balance_ok(corrected_text):
            block_reasons.append("unbalanced_brackets_or_parentheses")
        validation_issues.extend(blocking_validation_issues(corrected_text))
        residual_literals = residual_select_cstring_literals(corrected_text)
        if residual_literals:
            validation_issues.append(
                {
                    "code": "remaining_spanish_select_cstring_literal",
                    "severity": "high",
                    "matches": residual_literals,
                }
            )
        surface_hits = residual_surface_markers(corrected_text)
        if surface_hits:
            validation_issues.append(
                {
                    "code": "remaining_spanish_surface_marker",
                    "severity": "high",
                    "matches": surface_hits,
                }
            )

    if validation_issues:
        block_reasons.append("validation_or_residual_issue")

    composed = 1 if corrected_text and not block_reasons else 0
    closure_candidate = composed
    return {
        **row,
        "sentence_rule": source_key if composer else "missing",
        "composed": composed,
        "segment_closure_candidate": closure_candidate,
        "block_reason": ";".join(dict.fromkeys(block_reasons)),
        "token_delta_kind": token_delta_kind(current_text, corrected_text) if corrected_text else "",
        "corrected_text": corrected_text,
        "validation_issues": validation_issues,
    }


def report_paths(context_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    stem = f"{now_stamp()}_issue_select_cstring_object_pronoun_sentence_composer_context_{context_run_id}"
    return reports_dir / f"{stem}.txt", reports_dir / f"{stem}.csv", reports_dir / f"{stem}.jsonl"


def write_reports(rows: list[dict[str, Any]], txt_path: Path, csv_path: Path, jsonl_path: Path, context_run_id: int) -> None:
    block_counts = Counter(row["block_reason"] or "composed" for row in rows)
    token_delta_counts = Counter(row["token_delta_kind"] or "none" for row in rows)

    lines = [
        "Issue Select_CString object pronoun sentence composer checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Source context run id: {context_run_id}",
        "",
        "Summary:",
        f"- Candidate segments: {len(rows):,}",
        f"- Composer rules available: {sum(1 for row in rows if row['source_key'] in COMPOSERS):,}",
        f"- Composed: {sum(row['composed'] for row in rows):,}",
        f"- Segment closure candidates: {sum(row['segment_closure_candidate'] for row in rows):,}",
        f"- Blocked: {sum(1 for row in rows if not row['composed']):,}",
        "- Apply allowed: 0",
        "- Production release allowed: 0",
        "",
        "Token delta:",
    ]
    for kind, count in token_delta_counts.most_common():
        lines.append(f"- {kind}: {count:,}")
    lines.extend(["", "Block reasons:"])
    for reason, count in block_counts.most_common():
        lines.append(f"- {reason}: {count:,}")
    lines.extend(["", "Closure candidates:"])
    for row in [row for row in rows if row["segment_closure_candidate"]]:
        lines.append(
            f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} "
            f"token_delta={row['token_delta_kind']}"
        )
        lines.append(f"  current: {short(row.get('current_text'), 280)}")
        lines.append(f"  corrected: {short(row.get('corrected_text'), 280)}")
    lines.extend(["", "Blocked samples:"])
    for row in [row for row in rows if not row["composed"]][:30]:
        lines.append(
            f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} "
            f"reason={row['block_reason']}"
        )

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fieldnames = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "sentence_rule",
        "composed",
        "segment_closure_candidate",
        "block_reason",
        "token_delta_kind",
        "current_text",
        "corrected_text",
        "validation_issues_json",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{field: row.get(field) for field in fieldnames},
                    "validation_issues_json": json.dumps(row.get("validation_issues") or [], ensure_ascii=False),
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            payload = dict(row)
            payload["validation_issues_json"] = json.dumps(row.get("validation_issues") or [], ensure_ascii=False)
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main(*, context_run_id: int | None = None) -> dict[str, Any]:
    conn = db.connect()
    conn.row_factory = db.sqlite3.Row
    try:
        ensure_tables(conn)
        selected_context_run_id = context_run_id or latest_context_run_id(conn)
        source_rows = fetch_segments(conn, selected_context_run_id)
        rows = [classify(row) for row in source_rows]
        txt_path, csv_path, jsonl_path = report_paths(selected_context_run_id)
        write_reports(rows, txt_path, csv_path, jsonl_path, selected_context_run_id)

        source_queue_run_id = int(rows[0]["source_queue_run_id"]) if rows else 0
        block_counts = Counter(row["block_reason"] or "composed" for row in rows)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_object_pronoun_sentence_composer_runs (
                source_context_run_id,
                source_queue_run_id,
                rule_version,
                policy_name,
                policy_status,
                agent_key,
                candidate_segments,
                composer_rules_available,
                composed_count,
                segment_closure_candidates,
                blocked_count,
                block_counts_json,
                production_release_allowed,
                report_path,
                csv_path,
                jsonl_path,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                selected_context_run_id,
                source_queue_run_id,
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                AGENT_KEY,
                len(rows),
                sum(1 for row in rows if row["source_key"] in COMPOSERS),
                sum(row["composed"] for row in rows),
                sum(row["segment_closure_candidate"] for row in rows),
                sum(1 for row in rows if not row["composed"]),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                now,
            ),
        )
        run_id = int(cursor.lastrowid)

        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_select_cstring_object_pronoun_sentence_composer_items (
                    run_id,
                    source_context_run_id,
                    source_context_item_ids_json,
                    source_queue_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    sentence_rule,
                    composed,
                    segment_closure_candidate,
                    block_reason,
                    token_delta_kind,
                    current_text,
                    corrected_text,
                    validation_issues_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_context_run_id,
                    json.dumps(row["source_context_item_ids"], ensure_ascii=False),
                    row["source_queue_run_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["sentence_rule"],
                    row["composed"],
                    row["segment_closure_candidate"],
                    row["block_reason"],
                    row["token_delta_kind"],
                    row["current_text"],
                    row["corrected_text"],
                    json.dumps(row["validation_issues"], ensure_ascii=False),
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    print("[issue_select_cstring_object_pronoun_sentence_composer_checkpoint] Checkpoint generated")
    print(f"[issue_select_cstring_object_pronoun_sentence_composer_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_select_cstring_object_pronoun_sentence_composer_checkpoint] Run id: {run_id}")
    print(f"[issue_select_cstring_object_pronoun_sentence_composer_checkpoint] Source context run id: {selected_context_run_id}")
    print(f"[issue_select_cstring_object_pronoun_sentence_composer_checkpoint] Candidate segments: {len(rows):,}")
    print(f"[issue_select_cstring_object_pronoun_sentence_composer_checkpoint] Composer rules available: {sum(1 for row in rows if row['source_key'] in COMPOSERS):,}")
    print(f"[issue_select_cstring_object_pronoun_sentence_composer_checkpoint] Composed: {sum(row['composed'] for row in rows):,}")
    print(f"[issue_select_cstring_object_pronoun_sentence_composer_checkpoint] Segment closure candidates: {sum(row['segment_closure_candidate'] for row in rows):,}")
    print("[issue_select_cstring_object_pronoun_sentence_composer_checkpoint] Apply allowed: 0")
    print(f"[issue_select_cstring_object_pronoun_sentence_composer_checkpoint] Report: {txt_path}")
    print(f"[issue_select_cstring_object_pronoun_sentence_composer_checkpoint] CSV: {csv_path}")
    print(f"[issue_select_cstring_object_pronoun_sentence_composer_checkpoint] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "source_context_run_id": selected_context_run_id,
        "candidate_segments": len(rows),
        "composer_rules_available": sum(1 for row in rows if row["source_key"] in COMPOSERS),
        "composed": sum(row["composed"] for row in rows),
        "segment_closure_candidates": sum(row["segment_closure_candidate"] for row in rows),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a read-only Select_CString object-pronoun sentence composer checkpoint.")
    parser.add_argument("--context-run-id", type=int, default=None)
    args = parser.parse_args()
    main(context_run_id=args.context_run_id)
