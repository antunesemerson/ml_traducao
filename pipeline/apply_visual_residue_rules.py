from __future__ import annotations

import argparse
import re
from collections import Counter
from datetime import datetime

import db
import local_quality_validator
import suggest_translations
from auto_validate_segments import upsert_auto_confirmation


RULE_VERSION = "visual_residue_rules_v1"
AUTO_SCORE = 0.995
ACTIONABLE_CODES = {
    "spanish_punctuation",
    "spanish_residue",
    "spanish_residue_in_literal",
    "missing_space_after_token",
    "missing_space_before_token",
    "space_before_punctuation",
    "replacement_question_mark_mojibake",
    "utf8_mojibake_sequence",
    "gender_token_extra_suffix",
    "gender_token_extra_prefix",
    "gender_token_joined_to_word",
    "embedded_gender_token_fragment",
    "neutral_word_with_gender_token",
    "leading_gender_article_token",
    "unnatural_portuguese_fragment",
}

SPANISH_LEFT_QUOTES = ("Â«", "Ã‚Â«", "Ãƒâ€šÃ‚Â«")
SPANISH_RIGHT_QUOTES = ("Â»", "Ã‚Â»", "Ãƒâ€šÃ‚Â»")
SPANISH_INVERTED_MARKS = ("Â¿", "Â¡", "Ã‚Â¿", "Ã‚Â¡", "Ãƒâ€šÃ‚Â¿", "Ãƒâ€šÃ‚Â¡")
GENDER_TOKEN_EXTRA_SUFFIX_PATTERN = re.compile(
    r"(\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\])([ao])\b",
    re.IGNORECASE,
)
GENDER_TOKEN_JOINED_PATTERN = re.compile(
    r"(\[[^\]]*Custom\(\s*['\"]ES_(?:LeLa|LoLa|DelDela)['\"]\s*\)\])(?=[A-Za-z\u00c0-\u00ff])",
    re.IGNORECASE,
)
TRAILING_PERIOD_AFTER_QUOTED_FINAL_PUNCTUATION = re.compile(r'([.!?â€¦]")\.')
DOUBLE_SPACE_AFTER_FORMAT_MARKER = re.compile(r"(#X)\s{2,}")
SUBJECT_BEFORE_TO_BE_LITERAL_PATTERN = re.compile(
    r"(\[[^\]]*GetShortUIName[^\]]*\](?:\s+[A-Za-z\u00c0-\u00ff]+){0,2}\s+\[[^\]]*?(?:LocalPlayerString|Select_CString)\([^\]]*?)'vocÃª Ã©'",
    re.IGNORECASE,
)
IDENTICAL_SELECT_CSTRING_PATTERN = re.compile(
    r"\[Select_CString\(\s*[^,\]]+,\s*'([^']+)'\s*,\s*'\1'\s*\)\]",
    re.IGNORECASE,
)
MOJIBAKE_REPLACEMENTS = (
    ("\u00c3\u0081", "\u00c1"),
    ("\u00c3\u0080", "\u00c0"),
    ("\u00c3\u0082", "\u00c2"),
    ("\u00c3\u0083", "\u00c3"),
    ("\u00c3\u0087", "\u00c7"),
    ("\u00c3\u0089", "\u00c9"),
    ("\u00c3\u008a", "\u00ca"),
    ("\u00c3\u008d", "\u00cd"),
    ("\u00c3\u0093", "\u00d3"),
    ("\u00c3\u0094", "\u00d4"),
    ("\u00c3\u0095", "\u00d5"),
    ("\u00c3\u009a", "\u00da"),
    ("\u00c3\u00a0", "\u00e0"),
    ("\u00c3\u00a1", "\u00e1"),
    ("\u00c3\u00a2", "\u00e2"),
    ("\u00c3\u00a3", "\u00e3"),
    ("\u00c3\u00a7", "\u00e7"),
    ("\u00c3\u00a9", "\u00e9"),
    ("\u00c3\u00aa", "\u00ea"),
    ("\u00c3\u00ad", "\u00ed"),
    ("\u00c3\u00b3", "\u00f3"),
    ("\u00c3\u00b4", "\u00f4"),
    ("\u00c3\u00b5", "\u00f5"),
    ("\u00c3\u00ba", "\u00fa"),
    ("\u00c3\u00bc", "\u00fc"),
    ("\u00c2\u00ab", '"'),
    ("\u00c2\u00bb", '"'),
    ("\u00c2\u00bf", ""),
    ("\u00c2\u00a1", ""),
    ("\u00c2\u00a0", " "),
    ("\u00e2\u20ac\u00a6", "..."),
    ("\u00e2\u20ac\u0153", '"'),
    ("\u00e2\u20ac\ufffd", '"'),
    ("\u00e2\u20ac\u009d", '"'),
    ("\u00e2\u20ac\u201d", "-"),
)
EXACT_SUBSTRING_REPLACEMENTS = {
    "Sair para a Área de Trabalho": "Sair para Área de Trabalho",
    "Herdeiro do jogador": "Herdeiro do Jogador",
    "herdeiro do jogador": "Herdeiro do Jogador",
    "herdeiro jogável": "$game_concept_player_heir$",
    "herdeira jogável": "$game_concept_player_heir$",
    "Haz clic para ver": "Clique para ver",
    "Clic para ver": "Clique para ver",
    "Clic para dispensar": "Clique para dispensar",
    "'rechazaste', 'rechazó'": "'rejeitou', 'rejeitou'",
    "'rechazas', 'rechaza'": "'rejeita', 'rejeita'",
    "'te contentas', 'se contenta'": "'contenta-se', 'contenta-se'",
    "'posees', 'posee'": "'tem', 'tem'",
    "'pertenecéis', 'pertenecen'": "'pertencem', 'pertencem'",
    "'satisfecha', 'satisfecho'": "'satisfeit[recipient.Custom('ES_OA')]', 'satisfeit[recipient.Custom('ES_OA')]'",
    "#weak (rechazado)#!": "#weak (Rejeitado)#!",
    "#X rechazada#!": "#X rejeitada#!",
    "20 años": "20 anos",
    "'vuestros', 'sus'": "'seus', 'seus'",
    "vuestra emperatriz": "sua imperatriz",
    "vuestro emperador": "seu imperador",
    "No tiene": "Não tem",
    "no tiene": "não tem",
    "No tienes": "Não tem",
    "no tienes": "não tem",
    "#bold No#!": "#bold Não#!",
    "#bold no#!": "#bold não#!",
    "Tiene tierras": "Possui terras",
    "tiene tierras": "possui terras",
    "Tienes tierras": "Possui terras",
    "tienes tierras": "possui terras",
    "Concept( 'married', 'casarse' )": "Concept( 'married', 'casar-se' )",
    "Concept( 'scheme', 'intrigan' )": "Concept( 'scheme', 'conspiram' )",
    "Concept( 'best_friend', 'mejor amig' )": "Concept( 'best_friend', 'melhor amig' )",
    "'extranjera intrusa', 'extranjero intruso'": "'estrangeira intrusa', 'estrangeiro intruso'",
    "#weak (o [subject|lE] si eres ": "#weak (ou [subject|lE] se você for ",
    "@warning_icon! #X No puedes prohibirle a un [acclaimed_knight|El] que sea tu $knight_culture_player_no_tooltip_lowercase$ #!": "@warning_icon! #X Você não pode proibir que um [acclaimed_knight|El] sirva como seu $knight_culture_player_no_tooltip_lowercase$ #!",
    "#I Haz clic para abrir la lista de $game_concept_accolade_types$#!": "#I Clique para abrir a lista de $game_concept_accolade_types$#!",
    "O Pequeno Caçador [Select_CString( visiting_liege.IsFemale, 'la pequeña cazadora', 'el pequeño cazador' )]": "[Select_CString( visiting_liege.IsFemale, 'A pequena caçadora', 'O pequeno caçador' )]",
    "$EFFECT_LIST_BULLET$Seus": "$EFFECT_LIST_BULLET$ Seus",
    "$BULLET_WITH_TAB$Usar": "$BULLET_WITH_TAB$ Usar",
    "$BULLET_WITH_TAB$Atacar": "$BULLET_WITH_TAB$ Atacar",
    "s?o": "são",
    "Voc?": "Você",
    "voc?": "você",
    "at?": "até",
    "n?o": "não",
    "N?o": "Não",
    "r?pido": "rápido",
    "influ?ncia": "influência",
    "desfeitas imagin?rias": "desfeitas imaginárias",
    "te?ricos": "teóricos",
    "j?": "já",
    "vit?ria": "vitória",
    "ineleg?vel": "inelegível",
    "op??o": "opção",
    "receber?": "receberá",
    "escapar?": "escapará",
    "impress?o": "impressão",
    "vulner?veis": "vulneráveis",
    "dispon?vel": "disponível",
    "dispon?veis": "disponíveis",
    "'adúltera o fornicadora', 'adúltero o fornicador'": "'adúltera ou fornicadora', 'adúltero ou fornicador'",
    " ) : $VALUE": " ): $VALUE",
    "] : $VALUE": "]: $VALUE",
    "\nPuedes celebrar": "\nVocÃª pode realizar",
    "\npuedes celebrar": "\nvocÃª pode realizar",
    "\\nPuedes celebrar": "\\nVocÃª pode realizar",
    "\\npuedes celebrar": "\\nvocÃª pode realizar",
    "#T Ir a [Character.LocalPlayerString( 'tu', 'la' )] ubicaciÃ³n": "#T Ir para [Character.LocalPlayerString( 'sua', 'a' )] localizaÃ§Ã£o",
    "[CHARACTER.LocalPlayerString( 'Tu', '' )] $SUBJECT_TYPE$": "$SUBJECT_TYPE$",
    " en [": " em [",
    "#!VocÃª": "#! VocÃª",
    "#!Seus": "#! Seus",
    "#!Suas": "#! Suas",
    "#!Alterar": "#! Alterar",
    "#T Consejo": "#T Conselho",
    ")]envolvid": ")] envolvid",
    ")]convidad": ")] convidad",
    " no [Select_CString": " nÃ£o [Select_CString",
    " no [CHARACTER.LocalPlayerString": " nÃ£o [CHARACTER.LocalPlayerString",
    "#weak (o [duchy": "#weak (ou [duchy",
    "#EMP m\u00e1s#!": "#EMP mais#!",
    "LocalPlayerString( 'tu', '' )]herdeiro": "LocalPlayerString( 'seu', '' )] herdeiro",
    "LocalPlayerString( 'tu', '' )]rival": "LocalPlayerString( 'seu', '' )] rival",
    "LocalPlayerString( 'tu', 'la' )] inimiga": "LocalPlayerString( 'sua', 'a' )] inimiga",
    "LocalPlayerString( 'tu', 'la' )] nÃªmesis": "LocalPlayerString( 'sua', 'a' )] nÃªmesis",
    "LocalPlayerString( 'Tu propio', 'El' )] casamento": "LocalPlayerString( 'Seu prÃ³prio', 'O' )] casamento",
    "Select_CString(target.IsFemale, 'una anfitriona apreciada', 'un anfitriÃ³n apreciado' )": "Select_CString(target.IsFemale, 'uma anfitriÃ£ estimada', 'um anfitriÃ£o estimado' )",
    "Select_CString( TARGET_CHARACTER.IsFemale, 'la cÃ³nyuge', 'el cÃ³nyuge' )": "Select_CString( TARGET_CHARACTER.IsFemale, 'a cÃ´njuge', 'o cÃ´njuge' )",
    "Select_CString( TARGET_CHARACTER.IsFemale, 'la casamentera', 'el casamentero' )": "Select_CString( TARGET_CHARACTER.IsFemale, 'a casamenteira', 'o casamenteiro' )",
    "Select_CString( wise_woman_scope.IsFemale, 'a la sabia', 'al sabio' )": "Select_CString( wise_woman_scope.IsFemale, 'a sábia', 'o sábio' )",
    "Concept( 'debt', 'debes' )": "Concept( 'debt', 'dívida' )",
    "Concept('heir', 'hereder' )": "Concept('heir', 'herdeir' )",
    "Concept( 'family_member', 'miembro de mi familia')": "Concept( 'family_member', 'membro da minha família')",
    "Concept( 'tyrant', 'tiran' )": "Concept( 'tyrant', 'tiran' )",
    "#I Haz clic para ver detalles#!": "#I Clique para ver detalhes#!",
    "#I Clic": "#I Clique",
    "facciones": "facções",
    "#bold no#!": "#bold não#!",
    "@warning_icon! #X Sin casillas para guardar": "@warning_icon! #X Sem espaços para guardar",
    "Ya no puedes usar": "Você não pode mais usar",
    " ni #high": " nem #high",
    "No eres [house_head|lE]": "Você não é [house_head|lE]",
    "'tenéis', 'tienen'": "'têm', 'têm'",
    "'tendrás', 'tendrá'": "'terá', 'terá'",
    "'ayudarás', 'ayudará'": "'ajudará', 'ajudará'",
    "'adoras', 'adora'": "'adora', 'adora'",
    "'crees', 'cree'": "'acredita', 'acredita'",
    "'eres', 'es'": "'é', 'é'",
    "'Eres', 'Es'": "'É', 'É'",
    "'emergiste', 'emergió'": "'emergiu', 'emergiu'",
    "'estuviste', 'estuvo'": "'esteve', 'esteve'",
    "'haces', 'hace'": "'faz', 'faz'",
    "'pasas', 'pasa'": "'passa', 'passa'",
    "'portas', 'porta'": "'porta', 'porta'",
    "'sabes', 'sabe'": "'sabe', 'sabe'",
    "'serías', 'sería'": "'seria', 'seria'",
    "'sueles', 'suele'": "'costuma', 'costuma'",
    "'comprenderás', 'comprenderá'": "'compreenderá', 'compreenderá'",
    "'ves', 've'": "'vê', 'vê'",
    "[recipient.LocalPlayerString( 'Tu', 'La' )] [dynasty|lE][recipient.LocalPlayerString( '', 'Loc_ES_de_GetShortUIName' )]": "[recipient.LocalPlayerString( 'Sua', 'A' )] [dynasty|lE][recipient.LocalPlayerString( '', 'Loc_ES_de_GetShortUIName' )]",
    "certez[employer.Custom('ES_OA')]a": "certeza",
    "certez[employer.Custom('ES_OA')]": "certeza",
    "Select_CString( recipient.IsPlayer, 'os', 'le' )": "Select_CString( recipient.IsPlayer, 'lhe', 'lhe' )",
    "Select_CString( recipient.IsPlayer, 'vos', recipient.GetShortUIName )": "Select_CString( recipient.IsPlayer, 'você', recipient.GetShortUIName )",
    "Select_CString(CHARACTER.IsLocalPlayer,'tu personaje', CHARACTER.GetShortUIName)": "Select_CString(CHARACTER.IsLocalPlayer,'seu personagem', CHARACTER.GetShortUIName)",
    "Concept( 'travel', 'viaje' )": "Concept( 'travel', 'viagem' )",
    "Concept('travel', 'viaje')": "Concept('travel', 'viagem')",
    "Concept('traveling', 'el viaje' )": "Concept('traveling', 'a viagem' )",
    "#EMP con urgencia#!": "#EMP com urgência#!",
    "#EMP un par de motivos#!": "#EMP alguns motivos#!",
    "TARGET_CHARACTER.LocalPlayerString( 'tu ', '' )][Concept( 'vassal', 'vasall' )|E]": "TARGET_CHARACTER.LocalPlayerString( 'seu ', '' )][Concept( 'vassal', 'vassal' )|E]",
    "Concept( 'vassal', 'vasall' )|E][Character.Custom('ES_OA')]": "Concept( 'vassal', 'vassal' )|E][Character.Custom('ES_OA')]",
    "$EFFECT_LIST_BULLET$Terá": "$EFFECT_LIST_BULLET$ Terá",
    "#high você#! você ": "#high você#! ",
    "#high Voc\u00ea#! Voc\u00ea ": "#high Voc\u00ea#! ",
    "#high você#! completa": "#high você#! completar",
    "#high Voc\u00ea#! Completa": "#high Voc\u00ea#! Completar",
    "CHARACTER.LocalPlayerString( 'tus', 'las' )] exigências": "CHARACTER.LocalPlayerString( 'suas', 'as' )] exigências",
    "Se você declarar guerra a [TARGET_CHARACTER.GetUIName]será quebrada a [truce|lE] com [CHARACTER.GetSheHe].": "Se você declarar guerra a [TARGET_CHARACTER.GetUIName], a [truce|lE] será quebrada.",
    "A outros personagens não gostam": "Outros personagens não gostam",
    "#EMP solo acaba de empezar!#!": "#EMP está apenas começando!#!",
    "#EMP Tengo#! que me dirigir": "#EMP Preciso#! me dirigir",
    "#high Tú#!": "#high Você#!",
    "Se cancelará tu [activity|lE] de [coronation|lE].": "Sua [activity|lE] de [coronation|lE] será cancelada.",
    "en estos momentos": "atualmente",
    "Contienda ibérica": "Luta Ibérica",
    "#V legendaria#!": "#V lendária#!",
    "#V una#! vez": "#V uma#! vez",
    "#V tu#!": "#V seu#!",
    "aumenta en gran medida": "aumenta muito",
    "aumenta un poco": "aumenta um pouco",
    "#weak Este personaje no puede comenzar ni unirse a facciones.#!": "#weak Este personagem não pode iniciar nem se juntar a facções.#!",
    "#weak Resulta más fácil encarcelar a este personaje.#!": "#weak Este personagem é mais fácil de aprisionar.#!",
    "#EMP [spy.GetHerHim|U] miro con intención homicida.#!": "#EMP Olhe para [spy.GetHerHim] com intenção homicida.#!",
}
PERSISTENT_PHRASE_REPLACEMENTS = {
    "a la buena": "Ã  boa",
    "a la niÃ±a": "Ã  menina",
    "abatiste": "abateu",
    "abati\u00f3": "abateu",
    "agradecimientos": "agradecimentos",
    "cabeza de tu fe": "chefe da sua fÃ©",
    "conheÃ§a a ti mesmo": "conheÃ§a a si mesmo",
    "conhece a ti mesmo": "conhece a si mesmo",
    "coronaciÃ³n": "coroaÃ§Ã£o",
    "coronacion": "coroaÃ§Ã£o",
    "coronas": "coroas",
    "de la autora": "da autora",
    "de la sanadora": "da curandeira",
    "da\u00f1ada": "prejudicada",
    "da\u00f1ado": "prejudicado",
    "del autor": "do autor",
    "del sanador": "do curandeiro",
    "del seÃ±orÃ­o de": "do senhorio de",
    "delincuentes": "delinquentes",
    "dice": "diz",
    "dices": "diz",
    "detalles": "detalhes",
    "expusiste": "exp\u00f4s",
    "expuso": "exp\u00f4s",
    "efectos especiales": "efeitos especiais",
    "edificios especiales": "edif\u00edcios especiais",
    "edificios": "edif\u00edcios",
    "esas": "essas",
    "especiales": "especiais",
    "hija ileg\u00edtima": "filha ileg\u00edtima",
    "hijo ileg\u00edtimo": "filho ileg\u00edtimo",
    "hiciste": "fez",
    "hizo": "fez",
    "imagen": "imagem",
    "innobles": "ign\u00f3beis",
    "fue": "foi",
    "fuiste": "foi",
    "ganaste": "ganhou",
    "gan\u00f3": "ganhou",
    "la vendedora": "a vendedora",
    "el vendedor": "o vendedor",
    "la charlatana": "a charlat\u00e3",
    "el charlat\u00e1n": "o charlat\u00e3o",
    "la peque\u00f1a cazadora": "a pequena ca\u00e7adora",
    "el peque\u00f1o cazador": "o pequeno ca\u00e7ador",
    "la pregonera": "a pregoeira",
    "el pregonero": "o pregoeiro",
    "a la pregonera": "\u00e0 pregoeira",
    "al pregonero": "ao pregoeiro",
    "esta mendiga enferma": "esta mendiga doente",
    "este mendigo enfermo": "este mendigo doente",
    "naciste": "nasceu",
    "naci\u00f3": "nasceu",
    "pareces": "parece",
    "suya": "sua",
    "tuya": "sua",
    "t\u00fa": "voc\u00ea",
    "pasaste": "passou",
    "pas\u00f3": "passou",
    "perdiste": "perdeu",
    "perdi\u00f3": "perdeu",
    "otros efectos": "outros efeitos",
    "otros": "outros",
    "relaciones asociadas": "rela\u00e7\u00f5es associadas",
    "asociadas": "associadas",
    "reprobables o delincuentes": "reprov\u00e1veis ou delinquentes",
    "reprobables": "reprov\u00e1veis",
    "amistad": "amizade",
    "capaces en": "capazes em",
    "capaces em": "capazes em",
    "esto tendr\u00e1 efectos": "isso ter\u00e1 efeitos",
    "ninguna": "nenhuma",
    "ning\u00fan": "nenhum",
    "no se puede celebrar esta actividad": "n\u00e3o \u00e9 poss\u00edvel realizar esta atividade",
    "mantener relaciones extramatrimoniales": "manter rela\u00e7\u00f5es extraconjugais",
    "relaciones extramatrimoniales": "rela\u00e7\u00f5es extraconjugais",
    "macacos macacos": "macacos",
    "subyugaci\u00f3n": "subjuga\u00e7\u00e3o",
    "te convertiste": "tornou-se",
    "se convirti\u00f3": "tornou-se",
    "tambi\u00e9n debe echarme mucho de menos": "tamb\u00e9m deve sentir muito minha falta",
    "tendr\u00e1": "ter\u00e1",
    "tendr\u00e1s": "ter\u00e1",
    "teut\u00f3nica": "teut\u00f4nica",
    "tienes": "tem",
    "tiene": "tem",
    "vasallos": "vassalos",
    "al niÃ±o": "ao menino",
    "con el proyecto terminado": "com o projeto terminado",
    "con el proyecto fracasado": "com o projeto fracassado",
    "con la contribuciÃ³n aportada": "com a contribuiÃ§Ã£o fornecida",
    "contribuciÃ³n a": "contribuiÃ§Ã£o a",
    "concentraciÃ³n": "concentraÃ§Ã£o",
    "efectos de contribuciÃ³n": "efeitos de contribuiÃ§Ã£o",
    "efectos de gran proyecto": "efeitos de grande projeto",
    "se requiere a": "",
    "que asista como": "deve comparecer como",
    "asista como": "compareÃ§a como",
    "asaltando": "saqueando",
    "asaltos automÃ¡ticos": "saques automÃ¡ticos",
    "asaltos": "saques",
    "ya estÃ¡": "jÃ¡ estÃ¡",
    "esta posesiÃ³n": "esta posse",
    "creaste": "criou",
    "creÃ³": "criou",
    "crear tradiciÃ³n": "criar tradiÃ§Ã£o",
    "imponer tradiciÃ³n": "impor tradiÃ§Ã£o",
    "reemplazar tradiciÃ³n": "substituir tradiÃ§Ã£o",
    "imponer": "impor",
    "reemplazar": "substituir",
    "tradiciÃ³n": "tradiÃ§Ã£o",
    "crear regimiento nuevo": "criar novo regimento",
    "regimiento nuevo": "novo regimento",
    "reclutar a todos los hombres de armas": "recrutar todos os homens de armas",
    "crear": "criar",
    "no tienes": "vocÃª nÃ£o tem",
    "en curso": "em andamento",
    "puedes celebrar": "vocÃª pode realizar",
    "puedes celebrar[": "vocÃª pode realizar[",
    "o unirte a": "ou participar de",
    "una actividad": "uma atividade",
    "celebrar actividad": "realizar atividade",
    "involucrarse en la actividad": "envolver-se na atividade",
    "invitaciÃ³n a la actividad": "convite para a atividade",
    "ver la actividad": "ver a atividade",
    "convertir": "converter",
    "asistiendo": "participando",
    "asistir como": "comparecer como",
    "estÃ¡s": "estÃ¡",
    "'estÃ¡s'": "'estÃ¡'",
    "si estÃ¡": "se estÃ¡",
    "de viaje": "viajando",
    "no puede cargar mÃ¡s saque del": "nÃ£o pode carregar mais saque de",
    "asalto": "saque",
    "no se puede": "nÃ£o Ã© possÃ­vel",
    "no se pueden crear": "nÃ£o Ã© possÃ­vel criar",
    "no se pueden": "nÃ£o Ã© possÃ­vel",
    "asaltar": "saquear",
    "tu propio": "seu prÃ³prio",
    "a tu aliad": "seu/sua aliad",
    "mientras se combate": "durante o combate",
    "en movimiento": "em movimento",
    "en retirada": "em retirada",
    "en una retirada": "em retirada",
    "en uma": "em uma",
    "fuera del territorio propio": "fora do prÃ³prio territÃ³rio",
    "fuera del propio territorio": "fora do prÃ³prio territÃ³rio",
    "fuera de territorio propio": "fora do prÃ³prio territÃ³rio",
    "cambiar a": "mudar para",
    "cambiar el": "alterar",
    "cambiar": "alterar",
    "hacer": "fazer",
    "comenzar": "comeÃ§ar",
    "activar": "ativar",
    "desactivar": "desativar",
    "seleccionar": "selecionar",
    "activadas": "ativadas",
    "desactivadas": "desativadas",
    "tarea": "tarefa",
    "actividad": "atividade",
    "selecciona una ubicaciÃ³n para tu": "selecione um local para",
    "ubicaciÃ³n": "localizaÃ§Ã£o",
    "regresar a": "voltar para",
    "cuando puedas celebrar": "quando puder realizar",
    "haz clic para": "clique para",
    "no es ejÃ©rcito de": "nÃ£o Ã© exÃ©rcito de",
    "trueque": "escambo",
    "progreso del trueque": "progresso do escambo",
    "no puede cargar mÃ¡s saque de": "nÃ£o pode carregar mais saque de",
    "dentro de tu propio": "dentro do seu prÃ³prio",
    "el ejÃ©rcito": "o exÃ©rcito",
    "asaltante": "saqueador",
    "asaltantes": "saqueadores",
    "unir ejÃ©rcitos": "unir exÃ©rcitos",
    "una posesiÃ³n bajo": "uma posse sob",
    "bajo": "sob",
    "es mÃ¡s pequeÃ±o que la": "Ã© menor que a",
    "hombres": "homens",
    "quitar punto de reuniÃ³n": "remover ponto de reuniÃ£o",
    "no hay": "nÃ£o hÃ¡",
    "aquÃ­ no hay ninguna": "aqui nÃ£o hÃ¡ nenhuma",
    "aquÃ­ no hay suficientes [barter_goods|le] para": "aqui nÃ£o hÃ¡ [barter_goods|lE] suficientes para",
    "aquÃ­ no hay suficientes": "aqui nÃ£o hÃ¡ suficientes",
    "ninguna": "nenhuma",
    "con la que hacer": "com a qual fazer",
    "que pueda hacerse cargo de las": "que possa assumir as",
    "hacerse cargo de las": "assumir as",
    "este punto de reuniÃ³n": "este ponto de reuniÃ£o",
    "de este punto": "deste ponto",
    "de este ponto": "deste ponto",
    "no puedes": "vocÃª nÃ£o pode",
    "al no ser": "por nÃ£o ser",
    "cambiar tu": "alterar sua",
    "otro(s)": "mais",
    "porque contraerÃ­as demasiadas": "porque contrairia dÃ­vidas demais",
    "saque no disponible debido al": "saque indisponÃ­vel devido ao",
    "hay algunos filtros activos": "hÃ¡ alguns filtros ativos",
    "trueque reciente": "escambo recente",
    "la posesiÃ³n ha sido saqueada o han trocado en ella recientemente. podrÃ¡ saquearse o trocar de nuevo el": "a posse foi saqueada ou alvo de escambo recentemente. PoderÃ¡ ser saqueada ou alvo de escambo novamente em",
    "reciente": "recente",
    "la contribuciÃ³n mÃ¡xima no puede superar": "a contribuiÃ§Ã£o mÃ¡xima nÃ£o pode superar",
    "deudas": "dÃ­vidas",
    "casilla": "vaga",
    "casillas": "espa\u00e7os",
    "debe": "deve",
    "arrendarse": "ser arrendado",
    "busca uno para tu": "procure um para seu",
    "si no tienes": "se vocÃª nÃ£o tiver",
    "fuerza total": "forÃ§a total",
    "assalto a": "saque a",
    "aÃºn no implementado": "ainda nÃ£o implementado",
    "nombrar": "nomear",
    "gran guerra santa": "grande guerra santa",
    "reasignar tropas": "reatribuir tropas",
    "clic para seleccionar": "clique para selecionar",
    "pulsa crtl + clic del ratÃ³n": "pressione Ctrl + clique do mouse",
    "'puedes'": "'pode'",
    "'puede'": "'pode'",
    "tus fuerzas": "suas forÃ§as",
    "eres": "vocÃª Ã©",
    "'es'": "'Ã©'",
    "\"es\"": "\"Ã©\"",
    "activo": "ativo",
    "orden": "ordem",
    "es demasiado pronto para volver a cambiar de enfoque": "Ã© cedo demais para voltar a alterar o foco",
    "no [select_cstring": "nÃ£o [Select_CString",
    "el [army|le] de [barter|le] de": "o [army|lE] de [barter|lE] de",
    "intercambia": "troca",
    "a cambio de": "em troca de",
    "ganarÃ¡": "ganharÃ¡",
    "devolver tropas contratadas": "devolver tropas contratadas",
    "verte delante de tu": "se ver diante de seu/sua",
    "compuesto por tu": "composto por seu/sua",
    "preajuste de consejo": "predefiniÃ§Ã£o de conselho",
    "consejo": "conselho",
    " y ": " e ",
    "te unes": "vocÃª se junta",
    "se une": "se junta",
    "es incompatible con esta bÃºsqueda de personaje": "Ã© incompatÃ­vel com esta busca de personagem",
    "tu personaje": "seu personagem",
    "estÃ¡ en tu": "estÃ¡ em seu/sua",
    "dividir en dos": "dividir em dois",
    "no tienes [activities|le] en curso\npuedes celebrar": "vocÃª nÃ£o tem [activities|lE] em andamento\nVocÃª pode realizar",
    "no tienes [activities|le] en curso\\npuedes celebrar": "vocÃª nÃ£o tem [activities|lE] em andamento\\nVocÃª pode realizar",
    "#bold no#! pode celebrar": "#bold NÃ£o#! pode realizar",
    "efectos": "efeitos",
    "relaciones": "relaÃ§Ãµes",
    "restricciones": "restriÃ§Ãµes",
    "contribuciÃ³n": "contribuiÃ§Ã£o",
    "aÃ±os": "anos",
    "de tu seÃ±or": "do seu senhor",
    "tu propio": "seu prÃ³prio",
    "vasallo ocupa la": "vassalo ocupa a",
    "conceder un tÃ­tulo por encima del grado de": "conceder um tÃ­tulo acima do grau de",
    "invalidarÃ¡ a este": "invalidarÃ¡ este",
    "no puede conceder los tÃ­tulos seleccionados porque se quedarÃ­a": "nÃ£o pode conceder os tÃ­tulos selecionados porque ficaria",
    "una o mÃ¡s posesiones de": "uma ou mais posses de",
    "estÃ¡n": "estÃ£o",
    "devociÃ³n": "devoÃ§Ã£o",
    "el falso": "o falso",
    "el ganador": "o vencedor",
    "el hÃ©roe": "o herÃ³i",
    "el niÃ±o": "o menino",
    "el nuevo": "o novo",
    "el primero": "o primeiro",
    "el sabio": "o sÃ¡bio",
    "el santo": "o santo",
    "el seÃ±or": "o senhor",
    "el Ãºnico": "o Ãºnico",
    "en ruinas": "em ruÃ­nas",
    "esta crÃ­a": "esta menina",
    "esta mujer": "esta mulher",
    "este crÃ­o": "este menino",
    "este hombre": "este homem",
    "jefe de su casa": "chefe da sua casa",
    "hermana": "irmÃ£",
    "hermano": "irmÃ£o",
    "hermana de sangre tuya": "sua irmÃ£ de sangue",
    "hermano de sangre tuyo": "seu irmÃ£o de sangue",
    "amiga tuya": "sua amiga",
    "amigo tuyo": "seu amigo",
    "hermandad": "irmandade",
    "de sangre": "de sangue",
    "la buena": "a boa",
    "la capitana": "a capitÃ£",
    "la falsa": "a falsa",
    "la ganadora": "a vencedora",
    "la hermosa mujer": "a bela mulher",
    "la heroÃ­na": "a heroÃ­na",
    "la jefa": "a chefe",
    "la misma": "a mesma",
    "la niÃ±a": "a menina",
    "la nueva": "a nova",
    "la primera": "a primeira",
    "la sabia": "a sÃ¡bia",
    "la seÃ±ora": "a senhora",
    "la Ãºnica": "a Ãºnica",
    "las dos": "as duas",
    "las cazadoras": "as caÃ§adoras",
    "las mujeres": "as mulheres",
    "los dos": "os dois",
    "los cazadores": "os caÃ§adores",
    "los hombres": "os homens",
    "interacciones": "interaÃ§Ãµes",
    "peligro de la caza": "perigo da caÃ§a",
    "'ti'": "'vocÃª'",
    "\"ti\"": "\"vocÃª\"",
    "toda una mujer": "toda uma mulher",
    "todo un hombre": "todo um homem",
    "estÃ¡s considerando": "estÃ¡ considerando",
    "respeto": "respeito",
    "tu personaje": "seu personagem",
    "tu parte": "vocÃª",
    "tu persona": "vocÃª",
    "tu seÃ±orÃ­o portuguÃ©s": "seu senhorio portuguÃªs",
    "tuyo": "seu",
    "tuya": "sua",
    "tuyos": "seus",
    "tuyas": "suas",
    "suyo": "seu",
    "suya": "sua",
    "suyos": "seus",
    "suyas": "suas",
    "tuyo/a": "seu/sua",
    "aÃ±o": "ano",
    "circunferencia": "circunferÃªncia",
    "una adulta": "uma adulta",
    "una cazadora": "uma caÃ§adora",
    "una cazadora harapienta": "uma caÃ§adora maltrapilha",
    "una campesina": "uma camponesa",
    "campesina": "camponesa",
    "una eunuca": "uma eunuca",
    "una guerrera": "uma guerreira",
    "una heroÃ­na": "uma heroÃ­na",
    "una maestra": "uma mestra",
    "una mujer": "uma mulher",
    "una mujer andrajosa": "uma mulher maltrapilha",
    "una mujer santa": "uma mulher santa",
    "una narradora": "uma narradora",
    "una niÃ±a": "uma menina",
    "una vasalla": "uma vassala",
    "un adulto": "um adulto",
    "un campesino": "um camponÃªs",
    "campesino": "camponÃªs",
    "un cazador": "um caÃ§ador",
    "un cazador harapiento": "um caÃ§ador maltrapilho",
    "un delito": "um crime",
    "un eunuco": "um eunuco",
    "un guerrero": "um guerreiro",
    "un hermoso": "um belo",
    "un hombre": "um homem",
    "un hombre andrajoso": "um homem maltrapilho",
    "un hombre santo": "um homem santo",
    "un maestro": "um mestre",
    "un narrador": "um narrador",
    "un niÃ±o": "um menino",
    "un vasallo": "um vassalo",
    "solitaria": "solitÃ¡ria",
    "solitario": "solitÃ¡rio",
    "tributaria": "tributÃ¡ria",
    "tributario": "tributÃ¡rio",
    "anfitriona": "anfitriÃ£",
    "anfitriÃ³n": "anfitriÃ£o",
    "actualmente": "atualmente",
    "a muerte": "atÃ© a morte",
    "no a muerte": "nÃ£o letal",
    "aventurera": "aventureira",
    "aventurero": "aventureiro",
    "mercenaria": "mercenÃ¡ria",
    "mercenario": "mercenÃ¡rio",
    "seguirÃ¡": "seguirÃ¡",
    "trabajaste": "trabalhou",
    "trabajÃ³": "trabalhou",
}


def percent(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return part / total * 100


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def issue_codes(validation: dict) -> Counter:
    return Counter(issue["code"] for issue in validation["issues"])


def actionable_total(codes: Counter) -> int:
    return sum(total for code, total in codes.items() if code in ACTIONABLE_CODES)


def replace_with_case(match: re.Match) -> str:
    replacement = suggest_translations.PERSISTENT_SPANISH_RESIDUES[match.group(0).casefold()]
    source = match.group(0)
    if source.isupper():
        return replacement.upper()
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def apply_persistent_residues(value: str) -> tuple[str, int]:
    updated = value
    replacements = 0
    for source, replacement in EXACT_SUBSTRING_REPLACEMENTS.items():
        updated, count = updated.replace(source, replacement), updated.count(source)
        replacements += count
    for spanish_phrase, portuguese_phrase in sorted(
        PERSISTENT_PHRASE_REPLACEMENTS.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        pattern = re.compile(
            rf"(?<![A-Za-z\u00c0-\u00ff]){re.escape(spanish_phrase)}(?![A-Za-z\u00c0-\u00ff])",
            re.IGNORECASE,
        )

        def phrase_replacement(match: re.Match) -> str:
            matched = match.group(0)
            if matched[:1].isupper():
                return portuguese_phrase[:1].upper() + portuguese_phrase[1:]
            return portuguese_phrase

        updated, count = pattern.subn(phrase_replacement, updated)
        replacements += count
    for spanish_term in suggest_translations.PERSISTENT_SPANISH_RESIDUES:
        pattern = re.compile(rf"\b{re.escape(spanish_term)}\b", re.IGNORECASE)
        updated, count = pattern.subn(replace_with_case, updated)
        replacements += count
    return updated, replacements


def apply_spanish_punctuation(value: str, english_text: str | None) -> tuple[str, int]:
    updated = value
    changes = 0
    use_double_quotes = '"' in (english_text or "")

    for mark in SPANISH_INVERTED_MARKS:
        count = updated.count(mark)
        if count:
            updated = updated.replace(mark, "")
            changes += count

    left_replacement = '"' if use_double_quotes else ""
    right_replacement = '"' if use_double_quotes else ""
    for mark in SPANISH_LEFT_QUOTES:
        count = updated.count(mark)
        if count:
            updated = updated.replace(mark, left_replacement)
            changes += count
    for mark in SPANISH_RIGHT_QUOTES:
        count = updated.count(mark)
        if count:
            updated = updated.replace(mark, right_replacement)
            changes += count

    updated, cleanup_count = TRAILING_PERIOD_AFTER_QUOTED_FINAL_PUNCTUATION.subn(r"\1", updated)
    changes += cleanup_count
    return updated, changes


def apply_gender_token_fixes(value: str) -> tuple[str, int]:
    updated, suffix_count = GENDER_TOKEN_EXTRA_SUFFIX_PATTERN.subn(r"\1", value)
    updated, joined_count = GENDER_TOKEN_JOINED_PATTERN.subn(r"\1 ", updated)
    return updated, suffix_count + joined_count


def apply_contextual_literal_fixes(value: str) -> tuple[str, int]:
    updated, count = SUBJECT_BEFORE_TO_BE_LITERAL_PATTERN.subn(r"\1'Ã©'", value)
    updated, identical_count = IDENTICAL_SELECT_CSTRING_PATTERN.subn(r"\1", updated)
    before_cleanup = updated
    updated = updated.replace("#high você#! você ", "#high você#! ")
    updated = updated.replace("#high Você#! Você ", "#high Você#! ")
    updated = updated.replace("#high você#! completa", "#high você#! completar")
    updated = updated.replace("#high Você#! Completa", "#high Você#! Completar")
    updated = updated.replace("completarr", "completar")
    updated = updated.replace("Completarr", "Completar")
    cleanup_count = 1 if updated != before_cleanup else 0
    return updated, count + identical_count + cleanup_count


def normalize_mojibake_literals(value: str) -> tuple[str, int]:
    updated = value
    changes = 0
    for source, replacement in MOJIBAKE_REPLACEMENTS:
        count = updated.count(source)
        if count:
            updated = updated.replace(source, replacement)
            changes += count
    return updated, changes


def build_candidate(row) -> tuple[str | None, list[str]]:
    base_text = row["confirmed_text"] or row["old_text"] or row["spanish_text"]
    if not base_text:
        return None, []

    updated = str(base_text)
    rules: list[str] = []

    updated, count = apply_spanish_punctuation(updated, row["english_text"])
    if count:
        rules.append(f"spanish_punctuation:{count}")

    updated, count = apply_gender_token_fixes(updated)
    if count:
        rules.append(f"gender_token:{count}")

    updated, count = apply_persistent_residues(updated)
    if count:
        rules.append(f"persistent_residue:{count}")

    updated, count = apply_contextual_literal_fixes(updated)
    if count:
        rules.append(f"contextual_literal:{count}")

    before_context_cleanup = updated
    updated = updated.replace("não é mais é ", "não é mais ")
    updated = updated.replace("tornou-se em ", "tornou-se ")
    updated = updated.replace("Algo #EMP tem#! tem que", "Algo #EMP tem#! que")
    updated = updated.replace(" si você é ", " se você é ")
    updated = updated.replace("(o [subject|lE] si você é", "(ou [subject|lE] se você é")
    if updated != before_context_cleanup:
        rules.append("context_cleanup:1")

    updated, count = DOUBLE_SPACE_AFTER_FORMAT_MARKER.subn(r"\1 ", updated)
    if count:
        rules.append(f"spacing_cleanup:{count}")

    updated, count = normalize_mojibake_literals(updated)
    if count:
        rules.append(f"normalize_mojibake:{count}")

    if updated == base_text:
        return None, []
    return updated, rules


def load_rows(conn, start_after: int | None = None, path_like: str | None = None) -> list:
    return conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.spanish_text,
            s.english_text,
            s.old_text,
            sc.confirmed_text,
            sc.confirmation_level,
            COALESCE(sc.locked, 0) AS locked
        FROM source_segments s
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND (? IS NULL OR s.id > ?)
          AND COALESCE(sc.locked, 0) = 0
          AND COALESCE(sc.confirmed_text, s.old_text, s.spanish_text, '') != ''
          AND s.relative_path NOT LIKE 'dynasties/%'
          AND s.relative_path NOT LIKE 'names/%'
          AND s.relative_path NOT LIKE 'relationship_reasons%'
          AND s.source_key NOT LIKE 'dynn_%'
          AND (? IS NULL OR s.relative_path LIKE ?)
        ORDER BY s.id
        """,
        (start_after, start_after, path_like, path_like),
    ).fetchall()


def purge_invalid_visual_confirmations(conn, apply: bool, path_like: str | None = None) -> int:
    rows = conn.execute(
        """
        SELECT sc.segment_id, sc.confirmed_text
        FROM segment_confirmations sc
        JOIN source_segments s ON s.id = sc.segment_id
        WHERE sc.confirmation_source = 'visual_residue_rule'
          AND sc.locked = 0
          AND (? IS NULL OR s.relative_path LIKE ?)
        """,
        (path_like, path_like),
    ).fetchall()
    invalid_ids: list[int] = []
    for row in rows:
        validation = local_quality_validator.validate_text(row["confirmed_text"])
        codes = issue_codes(validation)
        if actionable_total(codes) > 0:
            invalid_ids.append(row["segment_id"])

    if apply and invalid_ids:
        placeholders = ", ".join("?" for _ in invalid_ids)
        conn.execute(
            f"""
            DELETE FROM segment_confirmations
            WHERE confirmation_source = 'visual_residue_rule'
              AND locked = 0
              AND segment_id IN ({placeholders})
            """,
            invalid_ids,
        )
    return len(invalid_ids)


def main(
    limit: int | None = None,
    apply: bool = False,
    scan_limit: int | None = None,
    start_after: int | None = None,
    path_like: str | None = None,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[apply_visual_residue_rules] Starting visual residue rule pass")
    print(f"[apply_visual_residue_rules] Rule version: {RULE_VERSION}")
    print(f"[apply_visual_residue_rules] Validator version: {local_quality_validator.RULE_VERSION}")
    print(f"[apply_visual_residue_rules] Apply: {apply}")
    print(f"[apply_visual_residue_rules] Limit: {limit if limit is not None else 'none'}")
    print(f"[apply_visual_residue_rules] Scan limit: {scan_limit if scan_limit is not None else 'none'}")
    print(f"[apply_visual_residue_rules] Start after: {start_after if start_after is not None else 'none'}")
    print(f"[apply_visual_residue_rules] Path filter: {path_like or 'none'}")

    inspected = 0
    changed = 0
    accepted = 0
    skipped = Counter()
    rule_counts = Counter()
    before_issue_counts = Counter()
    after_issue_counts = Counter()
    preview: list[tuple[dict, str, list[str], Counter, Counter]] = []
    invalid_visual_confirmations = 0

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        invalid_visual_confirmations = purge_invalid_visual_confirmations(conn, apply, path_like)
        if invalid_visual_confirmations and apply:
            conn.commit()
        rows = load_rows(conn, start_after, path_like)
        for row in rows:
            inspected += 1
            if scan_limit is not None and inspected > scan_limit:
                skipped["scan_limit_reached"] += 1
                break
            candidate, rules = build_candidate(row)
            if candidate is None:
                skipped["no_rule_change"] += 1
                continue
            changed += 1

            token_state, token_details = suggest_translations.token_status(row["spanish_text"], candidate)
            if token_state != "ok":
                skipped[f"token_{token_state}"] += 1
                continue

            base_text = row["confirmed_text"] or row["old_text"] or row["spanish_text"]
            before_validation = local_quality_validator.validate_text(base_text)
            after_validation = local_quality_validator.validate_text(candidate)
            before_codes = issue_codes(before_validation)
            after_codes = issue_codes(after_validation)
            before_actionable = actionable_total(before_codes)
            after_actionable = actionable_total(after_codes)

            if after_actionable >= before_actionable:
                skipped["no_actionable_improvement"] += 1
                continue
            if after_actionable > 0:
                skipped["partial_fix_remaining_issues"] += 1
                continue

            item = {
                "segment_id": row["segment_id"],
                "candidate_text": candidate,
                "candidate_source": "visual_residue_rule",
                "feedback_id": None,
            }
            accepted += 1
            rule_counts.update(rule.split(":", 1)[0] for rule in rules)
            before_issue_counts.update(before_codes)
            after_issue_counts.update(after_codes)
            if len(preview) < 50:
                preview.append((dict(row), candidate, rules, before_codes, after_codes))
            if apply:
                upsert_auto_confirmation(conn, item, AUTO_SCORE)

            if limit is not None and accepted >= limit:
                break

        if apply:
            conn.commit()

        total_segments = int(
            conn.execute("SELECT COUNT(*) FROM source_segments WHERE is_active = 1").fetchone()[0] or 0
        )
        confirmed_rows = conn.execute(
            """
            SELECT sc.confirmation_level, COUNT(*) AS total
            FROM segment_confirmations sc
            JOIN source_segments s ON s.id = sc.segment_id
            WHERE s.is_active = 1
            GROUP BY sc.confirmation_level
            """
        ).fetchall()

    confirmed = {row["confirmation_level"]: int(row["total"] or 0) for row in confirmed_rows}
    total_confirmed = sum(confirmed.values())
    elapsed = datetime.now() - started_at
    report_lines = [
        "Visual residue rule pass report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Validator version: {local_quality_validator.RULE_VERSION}",
        f"Apply: {apply}",
        f"Limit: {limit if limit is not None else 'none'}",
        f"Scan limit: {scan_limit if scan_limit is not None else 'none'}",
        f"Start after: {start_after if start_after is not None else 'none'}",
        f"Path filter: {path_like or 'none'}",
        "",
        "Summary:",
        f"- Segments inspected: {inspected}",
        f"- Segments changed by rules: {changed}",
        f"- Auto-confirmable fixes: {accepted}",
        f"- Applied confirmations: {accepted if apply else 0}",
        f"- Invalid prior visual confirmations {'purged' if apply else 'found'}: {invalid_visual_confirmations}",
        f"- Active segments: {total_segments}",
        f"- Confirmed after run: {total_confirmed} ({percent(total_confirmed, total_segments):.4f}%)",
        f"- Human confirmed: {confirmed.get('human_confirmed', 0)}",
        f"- Auto confirmed: {confirmed.get('auto_confirmed', 0)}",
        "",
        "Rule hits accepted:",
    ]
    report_lines.extend(f"- {rule}: {count}" for rule, count in rule_counts.most_common())
    if not rule_counts:
        report_lines.append("- none")

    report_lines.extend(["", "Before issue counts on accepted fixes:"])
    report_lines.extend(f"- {code}: {count}" for code, count in before_issue_counts.most_common())
    if not before_issue_counts:
        report_lines.append("- none")

    report_lines.extend(["", "After issue counts on accepted fixes:"])
    report_lines.extend(f"- {code}: {count}" for code, count in after_issue_counts.most_common())
    if not after_issue_counts:
        report_lines.append("- none")

    report_lines.extend(["", "Skipped reasons:"])
    report_lines.extend(f"- {reason}: {count}" for reason, count in skipped.most_common())
    if not skipped:
        report_lines.append("- none")

    report_lines.extend(["", "Preview:"])
    if not preview:
        report_lines.append("- No accepted fixes")
    for row, candidate, rules, before_codes, after_codes in preview:
        report_lines.extend(
            [
                f"- segment {row['segment_id']} | {row['relative_path']}::{row['source_key']}",
                f"  rules: {', '.join(rules)}",
                f"  before issues: {dict(before_codes)}",
                f"  after issues: {dict(after_codes)}",
                f"  before: {short(row['confirmed_text'] or row['old_text'] or row['spanish_text'])}",
                f"  after:  {short(candidate)}",
            ]
        )

    report_path = db.write_report(settings, "apply_visual_residue_rules", report_lines)
    print(f"[apply_visual_residue_rules] Segments inspected: {inspected}")
    print(f"[apply_visual_residue_rules] Segments changed by rules: {changed}")
    print(f"[apply_visual_residue_rules] Auto-confirmable fixes: {accepted}")
    print(f"[apply_visual_residue_rules] Applied confirmations: {accepted if apply else 0}")
    print(
        "[apply_visual_residue_rules] Invalid prior visual confirmations "
        f"{'purged' if apply else 'found'}: {invalid_visual_confirmations}"
    )
    print(f"[apply_visual_residue_rules] Report: {report_path}")
    print("[apply_visual_residue_rules] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preview or apply conservative visual residue fixes.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum accepted fixes to preview/apply.")
    parser.add_argument("--scan-limit", type=int, default=None, help="Maximum rows to inspect before writing a report.")
    parser.add_argument("--start-after", type=int, default=None, help="Only inspect rows with source segment id greater than this value.")
    parser.add_argument("--path-like", default=None, help="Optional SQL LIKE filter for source relative_path.")
    parser.add_argument("--apply", action="store_true", help="Write auto_confirmed rows. Default is report only.")
    args = parser.parse_args()
    main(limit=args.limit, apply=args.apply, scan_limit=args.scan_limit, start_after=args.start_after, path_like=args.path_like)
