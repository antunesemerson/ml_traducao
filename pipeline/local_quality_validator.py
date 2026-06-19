from __future__ import annotations

import re


RULE_VERSION = "local_quality_validator_v6"

WORD_PATTERN = re.compile(r"[A-Za-z\u00c0-\u00ff]+", re.UNICODE)
PROTECTED_TOKEN_PATTERN = re.compile(
    r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n"
)
STRING_LITERAL_PATTERN = re.compile(r"'([^']*)'|\"([^\"]*)\"")
STRAY_LEADING_QUESTION_PATTERN = re.compile(r"^\?\s*\[")
SPACE_BEFORE_PUNCTUATION_PATTERN = re.compile(r"\s+[,.;:!?]")
QUESTION_MARK_MOJIBAKE_PATTERN = re.compile(
    r"[A-Za-z\u00c0-\u00ff]\?[A-Za-z\u00c0-\u00ff]|"
    r"\b(?:Voc|voc|n|N|pr|Pr|dom|Dom|imp|Imp|m|M|f|F|s|S|cora|Cora|"
    r"pol|Pol|hist|Hist|vit|Vit|posi|Posi|for|For|futuras|gera)\?"
    r"|\b[jJ]\?(?=\s|[,.;:])"
    r"|\b[Hh]\?(?=\s+[A-Za-z\u00c0-\u00ff#\[])"
    r"|\b(?:ajud|aparecer|cuidar|continuar|intrometer|lev|pagar|pensar|precisar|"
    r"prorrog|questionar|suceder|tentar|ver)\?(?=(?:-[A-Za-z\u00c0-\u00ff]+|[,.;:]|\s+[a-z\u00c0-\u00ff#\[]))"
    r"|(?:(?<!\S)|(?<=[\"'(\[]))\?(?=\s+(?:a\s+lealdade|a\s+minha|com\s+o|"
    r"o\s+qu[aã]o|um|uma|minha|meu|sua|seu|viagem|ordem|hora|fundamental|"
    r"obtido|obtida|claro|longo|curto|respeitad[oa]|poss[ií]vel)\b)"
    r"|\b(?:conseguir|poder|custar|ficar|receber|ter|dar|tomar|ecoar|curvar|ir)\?(?=\s+[a-z\u00e0-\u00ff#\[])"
)
GENDER_SUFFIX_QUESTION_PATTERN = re.compile(
    r"\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO|XA)['\"]\s*\)\]$",
    re.IGNORECASE,
)

SPANISH_PUNCTUATION = (
    "\u00bf",
    "\u00a1",
    "\u00ab",
    "\u00bb",
    "\u00c2\u00bf",
    "\u00c2\u00a1",
    "\u00c2\u00ab",
    "\u00c2\u00bb",
)

SPANISH_RESIDUE_WORDS = {
    "aguantar",
    "alabemos",
    "aquel",
    "aquella",
    "aquellas",
    "aquellos",
    "actualmente",
    "alguien",
    "acción",
    "accion",
    "abroncaste",
    "abroncó",
    "abochornaste",
    "abochornó",
    "abandonaste",
    "abandonó",
    "aceptaste",
    "aceptó",
    "ajustaste",
    "ajustó",
    "admiraste",
    "admiró",
    "adoptaste",
    "adoptó",
    "auténtica",
    "auténtico",
    "autentica",
    "autentico",
    "apoyaste",
    "apoyó",
    "apreciaste",
    "apreció",
    "animaste",
    "animó",
    "admiración",
    "admiracion",
    "atentaste",
    "atentó",
    "atravesar",
    "aventurera",
    "aventurero",
    "cautiva",
    "cautivo",
    "cautivadora",
    "cautivador",
    "avanzaste",
    "avanzó",
    "casi",
    "años",
    "asesinaste",
    "asesinó",
    "asesina",
    "asesino",
    "asaltos",
    "calumniaste",
    "calumnió",
    "acudiste",
    "acudió",
    "brindaste",
    "brindó",
    "consolaste",
    "consoló",
    "congeniasteis",
    "congeniaron",
    "confiaste",
    "confió",
    "curaste",
    "curó",
    "demostraste",
    "demostró",
    "cediste",
    "cedió",
    "desafian",
    "desafían",
    "cabeza",
    "cambiaste",
    "cambió",
    "malnacid",
    "circunferencia",
    "contribuiste",
    "contribuyó",
    "comprenderás",
    "comprenderá",
    "con",
    "concentración",
    "concentracion",
    "consejo",
    "consumarse",
    "contribución",
    "contribucion",
    "cónyuge",
    "conyuge",
    "cosas",
    "coste",
    "contaste",
    "contó",
    "crees",
    "cree",
    "contienda",
    "coronacion",
    "coronación",
    "gran coronación",
    "coronación exigua",
    "coronación modesta",
    "coronación respetable",
    "coronación resplandeciente",
    "marinera anciana",
    "marinero anciano",
    "marinera anciana parlanchina",
    "marinero anciano parlanchín",
    "coronas",
    "común",
    "comun",
    "compañía",
    "compania",
    "públicamente",
    "cortesano",
    "cortesanos",
    "cortesana",
    "cortesanas",
    "cuidaste",
    "cuidó",
    "culpaste",
    "culpó",
    "cazador",
    "cazadora",
    "escribiste",
    "escribió",
    "creaste",
    "creó",
    "crear",
    "efectos",
    "crujido",
    "cría",
    "crio",
    "crío",
    "decision",
    "decisiones",
    "distancia",
    "decidiste",
    "decidió",
    "decidieron",
    "del",
    "dejaste",
    "dejó",
    "desagradable",
    "desapareció",
    "desaparecio",
    "demasiado",
    "deshonraste",
    "deshonró",
    "derrotaste",
    "derrotó",
    "defendiste",
    "defendió",
    "difamaste",
    "difamó",
    "disfrutaste",
    "disfrutó",
    "dio",
    "diste",
    "colgaste",
    "colgó",
    "conseguiste",
    "consiguió",
    "congeniaste",
    "congenió",
    "convenciste",
    "convenció",
    "entorpeciste",
    "entorpeció",
    "ella",
    "el",
    "en",
    "enamorase",
    "encontraste",
    "encontró",
    "empapaste",
    "empapó",
    "elegiste",
    "eligió",
    "eliminaste",
    "eliminó",
    "enseñaste",
    "enseñó",
    "abatiste",
    "abatió",
    "expusiste",
    "expuso",
    "entablasteis",
    "entablaron",
    "entablar",
    "entablas",
    "entabla",
    "estuviste",
    "estuvo",
    "eres",
    "estás",
    "exquisita",
    "exquisitas",
    "exquisito",
    "exquisitos",
    "festejaste",
    "festejó",
    "fuiste",
    "fuisteis",
    "fueron",
    "fue",
    "ganaras",
    "ganarás",
    "ganaste",
    "ganó",
    "ganado",
    "gracias",
    "gobernante",
    "gobernantes",
    "lealtad",
    "lejos",
    "jefe",
    "jefa",
    "gusano",
    "harías",
    "haría",
    "ha",
    "habla",
    "harapienta",
    "harapiento",
    "hacendado",
    "hacendados",
    "horrorizaste",
    "horrorizó",
    "hermana",
    "hermano",
    "hermandad",
    "sangre",
    "sibila",
    "sustituiste",
    "sustituyó",
    "hiciste",
    "hizo",
    "humillaste",
    "humilló",
    "impresionaste",
    "impresionó",
    "presionar",
    "inmundicia",
    "intratable",
    "invitado",
    "invitados",
    "intención",
    "intencion",
    "homicida",
    "interacciones",
    "interés",
    "interes",
    "interferiste",
    "interfirió",
    "maquillaje",
    "intentaste",
    "intentó",
    "intentarás",
    "intentará",
    "interrumpir",
    "irrumpiste",
    "irrumpió",
    "la",
    "las",
    "le",
    "llevaste",
    "llevó",
    "llegaste",
    "llegó",
    "largo",
    "locamente",
    "lo",
    "lograr",
    "los",
    "mendiga",
    "mendigo",
    "pareces",
    "miro",
    "mostraste",
    "mostró",
    "manipuló",
    "astutamente",
    "matrimonio",
    "mercenaria",
    "mercenario",
    "mejoraste",
    "mejoró",
    "regañaste",
    "regañó",
    "reducida",
    "revelaste",
    "reveló",
    "impediste",
    "impidió",
    "encarcelaste",
    "encarceló",
    "enfureciste",
    "enfureció",
    "entremezclan",
    "exterminaste",
    "exterminó",
    "coaccionaste",
    "coaccionó",
    "tomaste",
    "tomó",
    "incitaste",
    "incitó",
    "sedujiste",
    "sedujo",
    "desarrollaste",
    "desarrolló",
    "secuestraste",
    "secuestró",
    "entretenida",
    "entretenido",
    "seducida",
    "seducido",
    "residencia",
    "familia",
    "esas",
    "campesina",
    "campesino",
    "nueva",
    "nuevas",
    "obligaste",
    "negaste",
    "negó",
    "obligó",
    "ofreciste",
    "ofreció",
    "obligarle",
    "pestes",
    "pasas",
    "pasa",
    "paliza",
    "precio",
    "pie",
    "pillaste",
    "pilló",
    "pero",
    "placeres",
    "preguntas",
    "pregonera",
    "pregonero",
    "prohibidos",
    "oportunidad",
    "otra",
    "perderas",
    "perderás",
    "pertenece",
    "perdonaste",
    "perdonó",
    "preparaste",
    "preparó",
    "proclamaste",
    "proclamó",
    "proporcionaste",
    "proporcionó",
    "prestaste",
    "prestó",
    "proyecto",
    "puso",
    "jefes",
    "relaciones",
    "responsable",
    "restricciones",
    "mayor",
    "fuerza",
    "suerte",
    "sueles",
    "suele",
    "sabes",
    "hará",
    "habrá",
    "ello",
    "consecuencias",
    "creces",
    "lanzas",
    "confianza",
    "puede",
    "puedes",
    "rechaza",
    "rechazar",
    "rechazaste",
    "rechazó",
    "reconoció",
    "reconocio",
    "recibiste",
    "recibió",
    "realizaste",
    "realizó",
    "regalaste",
    "regaló",
    "rapace",
    "reforzaste",
    "reforzó",
    "rezaste",
    "rezó",
    "salvaste",
    "salvó",
    "arrestaste",
    "arrestó",
    "arrollaste",
    "arrolló",
    "donaste",
    "donó",
    "rendiste",
    "rindió",
    "sermoneaste",
    "sermoneó",
    "silenciaste",
    "silenció",
    "reconectaste",
    "reconectó",
    "rememoraste",
    "rememoró",
    "reprendiste",
    "reprendió",
    "seguiste",
    "siguió",
    "flechazo",
    "seguiras",
    "seguirás",
    "trabajaste",
    "trabajó",
    "lengua",
    "trabada",
    "tono",
    "inseguro",
    "siembra",
    "donde",
    "verdad",
    "vecina",
    "vecinas",
    "vecino",
    "vecinos",
    "vitriol",
    "inventó",
    "invento",
    "supposed",
    "sólo",
    "buscaba",
    "armonía",
    "armonia",
    "trataste",
    "trató",
    "tributaria",
    "tributario",
    "sanadora",
    "sanador",
    "servicio",
    "ridiculizaste",
    "ridiculizó",
    "revientacaballos",
    "retorciendose",
    "retorci\u00e9ndose",
    "situacion",
    "situaciones",
    "se mete",
    "solitaria",
    "solitario",
    "su",
    "suyo",
    "suya",
    "suyos",
    "suyas",
    "superare",
    "superaré",
    "superaste",
    "superó",
    "tan",
    "te",
    "compartiste",
    "compartió",
    "enviaste",
    "envió",
    "hart",
    "tus",
    "tienes",
    "tiene",
    "tengas",
    "tenga",
    "tengo",
    "serás",
    "tutor",
    "iste",
    "ió",
    "unes",
    "tuyo",
    "tuya",
    "tuyos",
    "tuyas",
    "tú",
    "haces",
    "hace",
    "emergiste",
    "emergió",
    "ayudarás",
    "ayudará",
    "serías",
    "sería",
    "año",
    "adoras",
    "tuviste",
    "tuvo",
    "y",
    "empezaste",
    "empezó",
    "ganaste",
    "ganó",
    "humillaste",
    "humilló",
    "ayudaste",
    "ayudó",
    "quedaste",
    "quedó",
    "indignaste",
    "indignó",
    "enfrentaste",
    "enfrentó",
    "llevaste",
    "llevó",
    "regalaste",
    "regaló",
    "rescataste",
    "rescató",
    "aprendiste",
    "aprendió",
    "alabaste",
    "alabó",
    "acusaste",
    "acusó",
    "destronarás",
    "destronará",
    "encarcelarás",
    "encarcelará",
    "fracasaste",
    "fracasó",
    "libraste",
    "libró",
    "expulsaste",
    "expulsó",
    "inauguraste",
    "inauguró",
    "insultaste",
    "insultó",
    "libraste",
    "libró",
    "recompensaste",
    "recompensó",
    "renunciaste",
    "renunció",
    "pagaste",
    "pagó",
    "unirás",
    "unirá",
    "un",
    "una",
    "acaba",
    "camina",
    "aportada",
    "asaltando",
    "asista",
    "arrendarse",
    "aquí",
    "activar",
    "activo",
    "casilla",
    "cambiar",
    "cambio",
    "delante",
    "compuesto",
    "comenzar",
    "debido",
    "debe",
    "desactivar",
    "ejércitos",
    "deudas",
    "disponible",
    "ejército",
    "es",
    "hacer",
    "gran",
    "ganará",
    "hay",
    "haz",
    "clic",
    "imponer",
    "nombrar",
    "ninguna",
    "orden",
    "disminuye",
    "hombres",
    "intercambia",
    "mientras",
    "propio",
    "posesión",
    "pueda",
    "punto",
    "reclutar",
    "regimiento",
    "requiere",
    "reciente",
    "regresar",
    "reemplazar",
    "reasignar",
    "selecciona",
    "seleccionar",
    "saquearse",
    "saqueaste",
    "saqueó",
    "pueden",
    "pulsa",
    "sucesión",
    "tradición",
    "trueque",
    "unidad",
    "viaje",
    "vasalla",
    "vasallo",
    "hereder",
    "eras",
    "robaste",
    "robó",
    "tu",
    "señorío",
    "personaje",
    "perreras",
    "hueste",
    "mano",
    "ubicación",
    "unes",
    "vislumbraste",
    "vislumbró",
    "viste",
    "vio",
    "odio",
    "participasteis",
    "participaron",
    "pasaste",
    "pasó",
    "prometiste",
    "prometió",
    "resultaste",
    "resultó",
}
SPANISH_RESIDUE_PHRASES = {
    "ávida cazadora",
    "ávido cazador",
    "distancia corta",
    "distancia bastante corta",
    "distancia intermedia",
    "distancia respetable",
    "coste vagamente aumentado",
    "coste ligeramente aumentado",
    "coste intermedio",
    "coste elevado",
    "por doquier",
    "excautiva",
    "excautivo",
    "plañido",
    "bastante lejos",
    "muy lejos",
    "extraordinariamente lejos",
    "antes de que",
    "de verdad",
    "te enzarzaste",
    "se enzarzó",
    "te hartaste",
    "se hartó",
    "te viste",
    "se vio",
    "te aprovechaste",
    "se aprovechó",
    "conheça a ti mesmo",
    "al niño",
    "amiga imaginaria",
    "amigo imaginario",
    "a la anciana",
    "a la pregonera",
    "al anciano",
    "al pregonero",
    "el charlatán",
    "la charlatana",
    "el vendedor",
    "la vendedora",
    "el pequeño cazador",
    "la pequeña cazadora",
    "este mendigo enfermo",
    "esta mendiga enferma",
    "ahora deja",
    "con el",
    "de tu señor",
    "el héroe",
    "el jefe",
    "el nuevo",
    "el primero",
    "el pregonero",
    "el sabio",
    "el santo",
    "el señor",
    "el único",
    "en ruinas",
    "la jefa",
    "estás considerando",
    "me ocuparé",
    "máxima devoción",
    "a muerte",
    "#bold no#!",
    "#emp mi#!",
    "mi postre favorito",
    "todas mis cosas",
    "contármelo",
    "contarmelo",
    "os hicisteis",
    "se hicieron",
    "os entregasteis",
    "se entregaron",
    "se trata de mi",
    "puntos en común",
    "puntos en comun",
    "placeres prohibidos",
    "no a muerte",
    "ocupe de",
    "respeto",
    "no conoces personalmente",
    "tu persona",
    "tu personaje",
    "tu pueblo",
    "tu parte",
    "tu señorío",
    "tamaño",
    "familiar cercano",
    "tiene tierras",
    "se metía",
    "nuestra apreciada invitada",
    "nuestro apreciado invitado",
    "hija ileg\u00edtima",
    "hijo ileg\u00edtimo",
    "efectos especiales",
    "edificios especiales",
    "otros efectos",
    "relaciones asociadas",
    "reprobables o delincuentes",
    "capaces en",
    "capaces em",
    "esto tendr\u00e1 efectos",
    "haz clic para ver detalles",
    "no se puede celebrar esta actividad",
    "mantener relaciones extramatrimoniales",
    "relaciones extramatrimoniales",
    "sin casillas para guardar",
    "tambi\u00e9n debe echarme mucho de menos",
    "ya no puedes usar",
    "tuyo/a",
    "ti",
    "solo se puede ganar oro una vez por posesión",
    "probabilidad equivalente a un sexto de tu",
    "los artistas básicos en cualquier corte respetable",
    "puede que le pillen",
    "unas cuantas muertes en la naturaleza",
    "ganancia de",
    "según el número de",
    "presentes en tu",
    "lo sabía",
    "poesía ci",
    "poesÃ­a ci",
    "muy animada",
    "muy animadas",
    "muy animado",
    "muy animados",
    "reduce a la mitad el coste del",
    "cualquier tipo de comandante",
}

SPANISH_RESIDUE_WORDS.update(
    {
        "dice",
        "dices",
        "da\u00f1ada",
        "da\u00f1ado",
        "esas",
        "especiales",
        "imagen",
        "naciste",
        "naci\u00f3",
        "otros",
        "asociadas",
        "agradecimientos",
        "amistad",
        "capaces",
        "casillas",
        "debes",
        "delincuentes",
        "detalles",
        "edificios",
        "innobles",
        "ninguna",
        "ning\u00fan",
        "teut\u00f3nica",
        "ten\u00e9is",
        "reprobables",
        "subyugaci\u00f3n",
        "sonido",
        "tambi\u00e9n",
        "tendr\u00e1",
        "tendr\u00e1s",
        "tienen",
    "vasallos",
    "ves",
    "ve",
        "suya",
        "tuya",
    }
)

DETECTION_MOJIBAKE_REPLACEMENTS = (
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
)


def normalize_detection_term(value: str) -> str:
    updated = value
    for source, replacement in DETECTION_MOJIBAKE_REPLACEMENTS:
        updated = updated.replace(source, replacement)
    return updated


SORTED_SPANISH_RESIDUE_PHRASES = tuple(
    sorted({normalize_detection_term(phrase) for phrase in SPANISH_RESIDUE_PHRASES} | SPANISH_RESIDUE_PHRASES)
)
SORTED_SPANISH_RESIDUE_WORDS = tuple(
    sorted(({normalize_detection_term(word) for word in SPANISH_RESIDUE_WORDS} | SPANISH_RESIDUE_WORDS) - {"te"})
)
SPANISH_LITERAL_ONLY_RESIDUES = {"te"}
SPANISH_RESIDUE_PHRASE_PATTERNS = tuple(
    (
        phrase,
        re.compile(
            rf"(?<![-A-Za-zÀ-ÿ]){r'\s+'.join(re.escape(part) for part in phrase.split())}(?![-A-Za-zÀ-ÿ])"
        ),
    )
    for phrase in SORTED_SPANISH_RESIDUE_PHRASES
)
SPANISH_RESIDUE_WORD_PATTERNS = tuple(
    (
        word,
        re.compile(rf"(?<![-A-Za-zÀ-ÿ]){re.escape(word)}(?![-A-Za-zÀ-ÿ])"),
    )
    for word in SORTED_SPANISH_RESIDUE_WORDS
)

ENGLISH_RESIDUE_PATTERNS = (
    ("in_place_itself", re.compile(r"\bin\s+[A-Za-z][A-Za-z' -]{2,}\s+itself\b", re.IGNORECASE)),
    ("itself", re.compile(r"\bitself\b", re.IGNORECASE)),
    ("loses_nothing_to", re.compile(r"\bloses?\s+nothing\s+to\b", re.IGNORECASE)),
    ("gains_nothing", re.compile(r"\bgains?\s+nothing\b", re.IGNORECASE)),
    ("will_action", re.compile(r"\bwill\s+(?:lose|gain|receive|be|become|not)\b", re.IGNORECASE)),
    ("you_auxiliary", re.compile(r"\byou\s+(?:are|will|have|cannot|can)\b", re.IGNORECASE)),
)

TOKEN_JOINED_TO_WORD_PATTERN = re.compile(
    r"(\]|\$[A-Za-z0-9_]+\$|#!)(?=[A-Za-z\u00c0-\u00ff])"
)
WORD_JOINED_TO_TOKEN_PATTERN = re.compile(
    r"(?<=[A-Za-z\u00c0-\u00ff])(?<!\\n)(\$[A-Za-z0-9_]+\$|#(?:EMP|V|P|N|bold|weak)\b)"
)
CYRILLIC_OR_MOJIBAKE_PATTERN = re.compile(r"[\u0400-\u04ff\ufffdŤťĂă]")
MOJIBAKE_SEQUENCE_PATTERN = re.compile(
    r"(?:"
    r"[\u00c2\u00c3][\u0080-\u00bf\u00a0-\u00bf]"
    r"|[\u00e2][\u0080-\u009f\u20ac][\u0080-\u00bf\u0152\u0153\u02dc]?"
    r"|[\u00c4\u00c5][\u0080-\u00bf\u00a0-\u017f]"
    r")"
)
GENDER_TOKEN_SUFFIX_PATTERN = re.compile(
    r"\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\](?=[ao]\b)",
    re.IGNORECASE,
)
GENDER_TOKEN_UPPERCASE_SUFFIX_PATTERN = re.compile(
    r"\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\][AO](?=\s)"
)
GENDER_TOKEN_PREFIX_PATTERN = re.compile(
    r"(?<=[A-Za-z\u00c0-\u00ff])[ao]\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\]",
    re.IGNORECASE,
)
LEADING_GENDER_ARTICLE_TOKEN_PATTERN = re.compile(
    r"(^|[\s.!?;:])([ao])\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\]",
    re.IGNORECASE,
)
EMBEDDED_GENDER_TOKEN_FRAGMENT_PATTERN = re.compile(
    r"[A-Za-z\u00c0-\u00ff]{2,}\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO|XA)['\"]\s*\)\](?=[A-Za-z\u00c0-\u00ff])",
    re.IGNORECASE,
)
BROKEN_PORTUGUESE_GENDER_TOKEN_WORD_PATTERN = re.compile(
    r"\bcer\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO|XA)['\"]\s*\)\]\s*teza\b",
    re.IGNORECASE,
)
LOCALIZATION_GENDER_FLOW_PATTERN = re.compile(
    r"\[[^\]]*Custom\(\s*['\"]ES_(?:LeLa|LoLa|DelDela)['\"]\s*\)\](?=[A-Za-z\u00c0-\u00ff])",
    re.IGNORECASE,
)
NEUTRAL_WORD_GENDER_TOKEN_PATTERN = re.compile(
    r"\b(?:inexperiente|experiente|inteligente|competente|paciente|obediente|prudente|brilhante|viajante)"
    r"\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\]",
    re.IGNORECASE,
)
UNNATURAL_PORTUGUESE_FRAGMENT_PATTERNS = (
    re.compile(r"\bagrupamento de s\b", re.IGNORECASE),
    re.compile(r"\bdesignar um você mesmo\b", re.IGNORECASE),
    re.compile(r"\bd \[[^\]]+\]", re.IGNORECASE),
    re.compile(r"\bpres\s+atrás\b", re.IGNORECASE),
    re.compile(r"\bviv\s+até\b", re.IGNORECASE),
    re.compile(r"(?:^|\\n|\s)eu\s+pisc\b", re.IGNORECASE),
    re.compile(r"\beu\s+impulso\s+meu\s+corcel\b", re.IGNORECASE),
    re.compile(r"\bvejo\s+m\s+rival\b", re.IGNORECASE),
    re.compile(r"\bo-que\s+est[Ã¡á]\b", re.IGNORECASE),
    re.compile(r"\best[Ã¡á]\s+#EMP\s+acontece\b", re.IGNORECASE),
    re.compile(r"\besta\s+em\b", re.IGNORECASE),
    re.compile(r"\bexcelendo\s+em\b", re.IGNORECASE),
    re.compile(r"\bse\s+divergir\b", re.IGNORECASE),
    re.compile(r"\bparade\s+de\b", re.IGNORECASE),
    re.compile(r"\besteve\s+\[[^\]]+\]\s+partes\b", re.IGNORECASE),
    re.compile(r"\bme\s+é\s+apresentado\s+uma\b", re.IGNORECASE),
    re.compile(r"\bsede\s+crescente\s+que\s+n[ãÃ£]o\s+poderia\s+saciar\b", re.IGNORECASE),
    re.compile(r"\bdefensabilidade\b", re.IGNORECASE),
    re.compile(r"\bainda\s+n\b", re.IGNORECASE),
    re.compile(r"\baproximo\s+a\s+\[[^\]]+\]", re.IGNORECASE),
    re.compile(r"#high\s+voc(?:ê|Ãª)#!\s+voc(?:ê|Ãª)\b", re.IGNORECASE),
    re.compile(r"\bcompletarr\b", re.IGNORECASE),
    re.compile(
        r"\[Select_CString\([^\]]*'voc(?:ê|Ãª)'[^\]]*\]\s+"
        r"\[Select_CString\([^\]]*'voc(?:ê|Ãª)\s+(?:é|Ã©)'",
        re.IGNORECASE,
    ),
)


def normalize(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.casefold().split())


def strip_tokens(value: str | None) -> str:
    if not value:
        return ""
    stripped = PROTECTED_TOKEN_PATTERN.sub(" ", value)
    stripped = re.sub(r"[$#@!|_:\[\].()0-9\\/-]+", " ", stripped)
    return " ".join(stripped.split())


def word_count(value: str | None) -> int:
    return len(WORD_PATTERN.findall(strip_tokens(value)))


def literal_texts(value: str | None) -> list[str]:
    if not value:
        return []
    literals: list[str] = []

    position = 0
    for token_match in PROTECTED_TOKEN_PATTERN.finditer(value):
        outside = value[position : token_match.start()]
        for match in STRING_LITERAL_PATTERN.finditer(outside):
            literals.append(match.group(1) if match.group(1) is not None else match.group(2))

        token = token_match.group(0)
        if token.startswith("[") and token.endswith("]"):
            command_name = token[1:].split("(", 1)[0].split("|", 1)[0].strip()
            base_name = command_name.split(".")[-1]
            token_literals = [
                match.group(1) if match.group(1) is not None else match.group(2)
                for match in STRING_LITERAL_PATTERN.finditer(token)
            ]
            if base_name == "Concept":
                literals.extend(token_literals[1:])
            else:
                literals.extend(token_literals)
        position = token_match.end()

    outside = value[position:]
    for match in STRING_LITERAL_PATTERN.finditer(outside):
        literals.append(match.group(1) if match.group(1) is not None else match.group(2))
    return literals


def is_technical_literal(value: str) -> bool:
    normalized = value.strip()
    if not normalized:
        return True
    if " " not in normalized and "_" in normalized:
        return True
    if re.fullmatch(r"[A-Z0-9_]+", normalized):
        return True
    if normalized.startswith("Loc_"):
        return True
    return False


def count_spanish_residue(value: str | None) -> tuple[int, list[str]]:
    if not value:
        return 0, []
    visible_value = PROTECTED_TOKEN_PATTERN.sub(" ", value)
    normalized = normalize(visible_value)
    found: list[str] = []
    for phrase, pattern in SPANISH_RESIDUE_PHRASE_PATTERNS:
        if pattern.search(normalized):
            found.append(phrase)
    for word, pattern in SPANISH_RESIDUE_WORD_PATTERNS:
        if pattern.search(normalized):
            found.append(word)
    return len(found), found


def count_english_residue(value: str | None) -> tuple[int, list[str]]:
    if not value:
        return 0, []
    visible_value = PROTECTED_TOKEN_PATTERN.sub(" ", value)
    found = [label for label, pattern in ENGLISH_RESIDUE_PATTERNS if pattern.search(visible_value)]
    return len(found), found


def count_literal_spanish_residue(value: str | None) -> tuple[int, list[str]]:
    found: list[str] = []
    for literal in literal_texts(value):
        if is_technical_literal(literal):
            continue
        normalized_literal = normalize(literal)
        if normalized_literal in SPANISH_LITERAL_ONLY_RESIDUES:
            found.append(normalized_literal)
        _, literal_found = count_spanish_residue(literal)
        found.extend(literal_found)
        if any(mark in literal for mark in SPANISH_PUNCTUATION):
            found.append("spanish_punctuation_in_literal")
    return len(found), sorted(set(found))


def validate_text(value: str | None) -> dict:
    issues: list[dict] = []
    text = value or ""
    words = word_count(text)

    marks = [mark for mark in SPANISH_PUNCTUATION if mark in text]
    if marks:
        issues.append(
            {
                "code": "spanish_punctuation",
                "severity": "high",
                "message": "Spanish inverted or angular punctuation remains.",
                "matches": marks[:10],
            }
        )

    if STRAY_LEADING_QUESTION_PATTERN.search(text):
        issues.append(
            {
                "code": "stray_leading_question_mark",
                "severity": "medium",
                "message": "Text starts with a likely leftover inverted Spanish question mark.",
                "matches": ["?["],
            }
        )

    if SPACE_BEFORE_PUNCTUATION_PATTERN.search(text):
        issues.append(
            {
                "code": "space_before_punctuation",
                "severity": "medium",
                "message": "Human text has an extra space before punctuation.",
            }
        )

    if CYRILLIC_OR_MOJIBAKE_PATTERN.search(text):
        issues.append(
            {
                "code": "mojibake_or_unexpected_script",
                "severity": "high",
                "message": "Unexpected mojibake or non-Latin script remains in text.",
            }
        )

    mojibake_sequences = sorted(set(MOJIBAKE_SEQUENCE_PATTERN.findall(text)))
    if mojibake_sequences:
        issues.append(
            {
                "code": "utf8_mojibake_sequence",
                "severity": "high",
                "message": "Likely UTF-8 mojibake remains in text.",
                "matches": mojibake_sequences[:20],
            }
        )

    mojibake_matches = []
    for match in QUESTION_MARK_MOJIBAKE_PATTERN.finditer(text):
        value = match.group(0)
        if value == "s?":
            prefix = text[max(0, match.start() - 120) : match.start()]
            if GENDER_SUFFIX_QUESTION_PATTERN.search(prefix):
                continue
        mojibake_matches.append(value)
    mojibake_matches = sorted(set(mojibake_matches))
    if mojibake_matches:
        issues.append(
            {
                "code": "replacement_question_mark_mojibake",
                "severity": "high",
                "message": "Likely accent mojibake remains as replacement question marks.",
                "matches": mojibake_matches[:20],
            }
        )

    residue_count, residues = count_spanish_residue(text)
    if residue_count:
        issues.append(
            {
                "code": "spanish_residue",
                "severity": "high" if residue_count >= 2 else "medium",
                "message": "Known Spanish residue remains in human text.",
                "matches": residues[:20],
            }
        )

    english_count, english_residues = count_english_residue(text)
    if english_count:
        issues.append(
            {
                "code": "english_residue",
                "severity": "high",
                "message": "Known English residue remains in localized text.",
                "matches": english_residues[:20],
            }
        )

    literal_count, literal_residues = count_literal_spanish_residue(text)
    if literal_count:
        issues.append(
            {
                "code": "spanish_residue_in_literal",
                "severity": "high",
                "message": "Translatable string literal inside a token still looks Spanish.",
                "matches": literal_residues[:20],
            }
        )

    if TOKEN_JOINED_TO_WORD_PATTERN.search(text):
        issues.append(
            {
                "code": "missing_space_after_token",
                "severity": "medium",
                "message": "A token is joined directly to following human text.",
            }
        )

    if WORD_JOINED_TO_TOKEN_PATTERN.search(text):
        issues.append(
            {
                "code": "missing_space_before_token",
                "severity": "medium",
                "message": "Human text is joined directly to a formatting token.",
            }
        )

    if GENDER_TOKEN_SUFFIX_PATTERN.search(text):
        issues.append(
            {
                "code": "gender_token_extra_suffix",
                "severity": "high",
                "message": "A gender token such as ES_OA is followed by an extra a/o suffix.",
            }
        )

    if GENDER_TOKEN_UPPERCASE_SUFFIX_PATTERN.search(text):
        issues.append(
            {
                "code": "gender_token_uppercase_suffix",
                "severity": "high",
                "message": "A gender token is followed by an uppercase suffix that likely starts a new sentence.",
            }
        )

    if GENDER_TOKEN_PREFIX_PATTERN.search(text):
        issues.append(
            {
                "code": "gender_token_extra_prefix",
                "severity": "high",
                "message": "A word before a gender token such as ES_OA already ends in a/o.",
            }
        )

    if LEADING_GENDER_ARTICLE_TOKEN_PATTERN.search(text):
        issues.append(
            {
                "code": "leading_gender_article_token",
                "severity": "high",
                "message": "A standalone a/o before ES_OA/ES_AO is likely a broken Spanish gender construction.",
            }
        )

    if EMBEDDED_GENDER_TOKEN_FRAGMENT_PATTERN.search(text):
        issues.append(
            {
                "code": "embedded_gender_token_fragment",
                "severity": "high",
                "message": "A gender token is embedded inside a human word fragment.",
            }
        )

    if BROKEN_PORTUGUESE_GENDER_TOKEN_WORD_PATTERN.search(text):
        issues.append(
            {
                "code": "broken_portuguese_gender_token_word",
                "severity": "high",
                "message": "A gender token splits a Portuguese word that should not be gendered.",
            }
        )

    if LOCALIZATION_GENDER_FLOW_PATTERN.search(text):
        issues.append(
            {
                "code": "gender_token_joined_to_word",
                "severity": "medium",
                "message": "A gender/localization token is joined to the next word.",
            }
        )

    if NEUTRAL_WORD_GENDER_TOKEN_PATTERN.search(text):
        issues.append(
            {
                "code": "neutral_word_with_gender_token",
                "severity": "high",
                "message": "A gender-neutral Portuguese word is followed by an unnecessary ES_OA/ES_AO token.",
            }
        )

    unnatural_fragments = [
        pattern.pattern for pattern in UNNATURAL_PORTUGUESE_FRAGMENT_PATTERNS if pattern.search(text)
    ]
    if unnatural_fragments:
        issues.append(
            {
                "code": "unnatural_portuguese_fragment",
                "severity": "medium",
                "message": "Known unnatural Portuguese fragment remains in candidate text.",
                "matches": unnatural_fragments,
            }
        )

    high_count = sum(1 for issue in issues if issue["severity"] == "high")
    medium_count = sum(1 for issue in issues if issue["severity"] == "medium")

    confidence_cap = 0.99
    if words >= 70:
        confidence_cap = min(confidence_cap, 0.92)
    elif words >= 30:
        confidence_cap = min(confidence_cap, 0.95)

    if high_count:
        confidence_cap = min(confidence_cap, 0.79 if words >= 30 else 0.88)
    elif medium_count:
        confidence_cap = min(confidence_cap, 0.90 if words >= 30 else 0.94)

    auto_approval_blocked = high_count > 0 or medium_count > 0 or words >= 70
    return {
        "rule_version": RULE_VERSION,
        "word_count": words,
        "issues": issues,
        "issue_count": len(issues),
        "high_issue_count": high_count,
        "medium_issue_count": medium_count,
        "confidence_cap": confidence_cap,
        "auto_approval_blocked": auto_approval_blocked,
    }
