from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import short


RULE_VERSION = "auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_v1"
SELECT_CSTRING_SUBPOLICY = "select_cstring_invariant_ptbr_verb"
SELECT_CSTRING_BUCKET = "review_select_cstring_change"
GLOSSARY_LABEL_SUBPOLICY = "glossary_visible_label_ptbr_translation"
GLOSSARY_LABEL_BUCKET = "review_mixed_token_change"
ES_DELDELA_LITERAL_DE_SUBPOLICY = "es_deldela_literal_de_ptbr_repair"
ES_DELDELA_LITERAL_DE_BUCKET = "review_gender_token_change"
DYNAMIC_SCOPE_HELPER_SUBPOLICY = "dynamic_scope_ptbr_helper_neutralization"
DYNAMIC_SCOPE_HELPER_BUCKET = "review_dynamic_scope_change"
SELECT_CSTRING_DIRECT_NAME_SUBPOLICY = "select_cstring_direct_name_reference"
SELECT_CSTRING_DIRECT_NAME_BUCKET = SELECT_CSTRING_BUCKET
LOCALPLAYERSTRING_WAR_JOIN_SUBPOLICY = "localplayerstring_war_join_neutralization"
LOCALPLAYERSTRING_WAR_JOIN_BUCKET = DYNAMIC_SCOPE_HELPER_BUCKET
ES_HELPER_NARRATIVE_SUBPOLICY = "es_helper_narrative_neutralization"
ES_HELPER_NARRATIVE_BUCKETS = (
    DYNAMIC_SCOPE_HELPER_BUCKET,
    ES_DELDELA_LITERAL_DE_BUCKET,
    GLOSSARY_LABEL_BUCKET,
)
HOSTAGE_CONTEXT_SUBPOLICY = "hostage_context_neutralization"
HOSTAGE_CONTEXT_BUCKETS = (
    DYNAMIC_SCOPE_HELPER_BUCKET,
    GLOSSARY_LABEL_BUCKET,
)
SHORT_DYNAMIC_VERB_SUBPOLICY = "short_dynamic_spanish_verb_neutralization"
SHORT_DYNAMIC_VERB_BUCKET = SELECT_CSTRING_BUCKET
HUNT_ACTIVITY_SELECT_CSTRING_SUBPOLICY = "hunt_activity_select_cstring_neutralization"
HUNT_ACTIVITY_SELECT_CSTRING_BUCKET = SELECT_CSTRING_BUCKET
CORONATION_TITLE_ES_HELPER_SUBPOLICY = "coronation_title_es_helper_neutralization"
CORONATION_TITLE_ES_HELPER_BUCKET = ES_DELDELA_LITERAL_DE_BUCKET
NICKNAME_WHISPERER_SELECT_CSTRING_SUBPOLICY = "nickname_whisperer_select_cstring_neutralization"
NICKNAME_WHISPERER_SELECT_CSTRING_BUCKET = SELECT_CSTRING_BUCKET
SINGLE_COMBAT_VICTOR_NAME_SUBPOLICY = "single_combat_victor_name_pronoun_neutralization"
SINGLE_COMBAT_VICTOR_NAME_BUCKET = DYNAMIC_SCOPE_HELPER_BUCKET
TOUR_TITLE_POSSESSIVE_SUBPOLICY = "tour_title_gendered_possessive_neutralization"
TOUR_TITLE_POSSESSIVE_BUCKET = SELECT_CSTRING_BUCKET
EP3_TRAVEL_TITLE_ADJECTIVE_SUBPOLICY = "ep3_travel_title_adjective_alignment"
EP3_TRAVEL_TITLE_ADJECTIVE_BUCKET = DYNAMIC_SCOPE_HELPER_BUCKET
SUBPOLICY_BUCKETS = {
    SELECT_CSTRING_SUBPOLICY: SELECT_CSTRING_BUCKET,
    GLOSSARY_LABEL_SUBPOLICY: GLOSSARY_LABEL_BUCKET,
    ES_DELDELA_LITERAL_DE_SUBPOLICY: ES_DELDELA_LITERAL_DE_BUCKET,
    DYNAMIC_SCOPE_HELPER_SUBPOLICY: DYNAMIC_SCOPE_HELPER_BUCKET,
    SELECT_CSTRING_DIRECT_NAME_SUBPOLICY: SELECT_CSTRING_DIRECT_NAME_BUCKET,
    LOCALPLAYERSTRING_WAR_JOIN_SUBPOLICY: LOCALPLAYERSTRING_WAR_JOIN_BUCKET,
    ES_HELPER_NARRATIVE_SUBPOLICY: ES_HELPER_NARRATIVE_BUCKETS,
    HOSTAGE_CONTEXT_SUBPOLICY: HOSTAGE_CONTEXT_BUCKETS,
    SHORT_DYNAMIC_VERB_SUBPOLICY: SHORT_DYNAMIC_VERB_BUCKET,
    HUNT_ACTIVITY_SELECT_CSTRING_SUBPOLICY: HUNT_ACTIVITY_SELECT_CSTRING_BUCKET,
    CORONATION_TITLE_ES_HELPER_SUBPOLICY: CORONATION_TITLE_ES_HELPER_BUCKET,
    NICKNAME_WHISPERER_SELECT_CSTRING_SUBPOLICY: NICKNAME_WHISPERER_SELECT_CSTRING_BUCKET,
    SINGLE_COMBAT_VICTOR_NAME_SUBPOLICY: SINGLE_COMBAT_VICTOR_NAME_BUCKET,
    TOUR_TITLE_POSSESSIVE_SUBPOLICY: TOUR_TITLE_POSSESSIVE_BUCKET,
    EP3_TRAVEL_TITLE_ADJECTIVE_SUBPOLICY: EP3_TRAVEL_TITLE_ADJECTIVE_BUCKET,
}
PTBR_INVARIANT_VERBS = {
    "está",
    "ganha",
    "perde",
    "expandiu",
    "dispensa",
    "deixará",
    "gasta",
    "entrega",
    "usa",
    "possui",
}
SELECT_CSTRING_LITERAL_RE = re.compile(
    r"Select_CString\(\s*[^,]+,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)",
    re.IGNORECASE,
)
SELECT_CSTRING_LOCALPLAYER_LITERAL_RE = re.compile(
    r"Select_CString\(\s*([A-Za-z_][\w.]*)\.IsLocalPlayer\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)",
    re.IGNORECASE,
)
GLOSSARY_LITERAL_RE = re.compile(
    r"Glossary\(\s*'([^']*)'\s*,\s*'([^']*)'\s*\)",
    re.IGNORECASE,
)
ES_DELDELA_HELPER_RE = re.compile(
    r"^\[([^\]]+?)\.Custom\('ES_DelDela'\)\]$",
    re.IGNORECASE,
)
ES_ONLY_EL_SHORT_UI_RE = re.compile(
    r"\[([^\]]+?)\.Custom\('ES_only_el_GetShortUIName'\)(\|U)?\]",
    re.IGNORECASE,
)
LOCAL_PLAYER_STRING_RE = re.compile(
    r"\[([^\]]+?)\.LocalPlayerString\(\s*'([^']*)'\s*,\s*'([^']*)'\s*\)\]",
    re.IGNORECASE,
)
SELECT_CSTRING_DIRECT_NAME_RE = re.compile(
    r"\[Select_CString\(\s*([A-Za-z_][\w.]*)\.IsLocalPlayer\s*,\s*'([^']*)'\s*,\s*([A-Za-z_][\w.]*)\.GetShortUIName\s*\)\]",
    re.IGNORECASE,
)
DIRECT_SHORT_UI_TOKEN_RE = re.compile(
    r"^\[([A-Za-z_][\w.]*)\.GetShortUIName(?:NoTooltip)?(?:\|[^\]]+)?\]$",
    re.IGNORECASE,
)
ES_HELPER_TOKEN_RE = re.compile(
    r"^\[([^\]]+?)\.Custom\('ES_(OA|XA|EA|ElLa|DelDela|AlAla)'\)\]$",
    re.IGNORECASE,
)
ES_HELPER_IN_TEXT_RE = re.compile(r"\[[^\]]+?\.Custom\('ES_[^']+'\)\]", re.IGNORECASE)
ALLOWED_ES_NARRATIVE_NONHELPER_RE = re.compile(
    r"^\[[^\]]+\.(?:GetSheHe|GetHerHim|GetTitledFirstName(?:NoTooltip)?|GetTitleAsName(?:NoTooltip)?|Custom2\('RelationToMeShort')",
    re.IGNORECASE,
)
SHORT_DYNAMIC_VERB_PATTERNS = {
    "GREAT_PROJECT_FUNDED_CONTRIBUTION_TT": {
        "relative_path": "great_projects/great_project_l_spanish.yml",
        "scope": "Character",
        "normalized_literals": ("aportaste", "aporto"),
        "required_tokens": ("[Character.GetUIName]",),
        "required_phrases": ("foi financiada por",),
        "english_alignment": "plain_required_tokens",
        "english_required_tokens": ("[Character.GetUIName]",),
    },
    "diarch_revoke_title_interaction.tt.gain_title": {
        "relative_path": "interactions/diarch_interactions_l_spanish.yml",
        "scope": "actor",
        "normalized_literals": ("ganas", "gana"),
        "required_tokens": ("[actor.GetShortUIName|U]", "[target.GetName|V]", "[vassals|lE]"),
        "required_phrases": ("ganha",),
        "english_alignment": "parallel_select_cstring",
        "english_required_tokens": ("[actor.GetShortUIName|U]", "[target.GetName|V]"),
    },
}
HUNT_ACTIVITY_SELECT_CSTRING_PATTERNS = {
    "hunt_activity_chase_tt": {
        "relative_path": "activities/hunt_activity_l_spanish.yml",
        "scope": "host",
        "normalized_literals": ("tentara", "tentara"),
        "required_tokens": (
            "[host.GetShortUIName|U]",
            "[activity.Custom('GetAnimalType')]",
            "[hunt_dangerous|lE]",
        ),
        "required_markers": ("#V encurralar#!", "#weak (lan"),
        "english_markers": ("will attempt to", "corner", "[activity.Custom('GetAnimalType')]"),
    },
    "hunt_activity_captive_tt": {
        "relative_path": "activities/hunt_activity_l_spanish.yml",
        "scope": "host",
        "normalized_literals": ("tentara", "tentara"),
        "required_tokens": (
            "[host.GetShortUIName|U]",
            "[activity.Custom('GetAnimalType')]",
            "[hunt_dangerous|lE]",
        ),
        "required_markers": ("libertar e abater", "#V em cativeiro#!", "#weak (lan"),
        "english_markers": ("will attempt to", "release and slay", "[activity.Custom('GetAnimalType')]"),
    },
}
CORONATION_TITLE_ES_HELPER_PATTERNS = {
    "guest_intent_coronation_events.0100.desc.magnificence_loss_subtle": {
        "relative_path": "dlc/ach/dlc_ach_guest_intent_coronation_events_l_spanish.yml",
        "required_missing_tokens": (
            "[ROOT.Char.Custom('ES_AlAla')]",
            "[ROOT.Char.Custom('ES_ElLa')]",
            "[ROOT.Char.Custom('ES_OA')]",
        ),
        "required_corrected_tokens": (
            "[petition_sender.GetSheHe]",
            "[ROOT.Char.GetTitleAsName|U]",
            "[ROOT.Char.GetTitleAsNameNoTooltip|l]",
        ),
        "required_corrected_markers": (
            "em perfeita solidao",
            "infestada",
            "conversas desleais",
        ),
        "english_markers": (
            "[ROOT.Char.GetTitleAsName]",
            "[ROOT.Char.GetTitleAsNameNoTooltip]",
            "rife",
            "disloyal talk",
        ),
        "spanish_residue_markers": (
            "plagada",
            "solzinho",
        ),
    },
}
NICKNAME_WHISPERER_SELECT_CSTRING_PATTERNS = {
    "nick_the_whisperer_desc": {
        "relative_path": "nicknames_l_spanish.yml",
        "scope": "CHARACTER",
        "normalized_literals": ("passa", "passa"),
        "required_missing_tokens": (
            "[CHARACTER.Custom('ES_OA')]",
            "[Select_CString( CHARACTER.IsLocalPlayer, '<TEXT>', '<TEXT>' )]",
        ),
        "required_confirmed_tokens": ("[CHARACTER.Custom('ES_OA')]",),
        "required_corrected_tokens": ("[CHARACTER.GetShortUINameNoTooltipNoFormat]",),
        "required_corrected_markers": (
            "recebeu esse apelido",
            "falando em voz baixa",
            "apropriado",
        ),
        "english_markers": (
            "so-called",
            "speaking softly",
            "seemly",
        ),
    },
}
SINGLE_COMBAT_VICTOR_NAME_PATTERNS = {
    "single_combat.0031.desc.sc_attacker.mocking_boast": {
        "relative_path": "single_combat_events_l_spanish.yml",
        "required_extra_tokens": (
            "[sc_victor.GetFirstNameNoTooltip]",
            "[sc_victor.GetFirstNameNoTooltip]",
        ),
        "english_pronoun_tokens": (
            "[sc_victor.GetSheHe]",
            "[sc_victor.GetHerHim]",
        ),
        "required_corrected_markers": (
            "nao esta sem razao",
            "#emp exatamente#!",
            "a oportunidade para",
        ),
        "required_confirmed_markers": (
            "ele nao esta errado",
            "lhe da #emp justo#!",
        ),
        "required_quote_markers": (
            "construir uma reputacao",
            "matar alguem famoso",
        ),
    },
}
TOUR_TITLE_POSSESSIVE_PATTERNS = {
    "tour_grounds_events.3004.title": {
        "relative_path": "event_localization/activities/tour_events_l_spanish.yml",
        "required_extra_tokens": (
            "[Select_CString( visiting_liege.IsFemale, '<TEXT>', '<TEXT>' )]",
        ),
        "required_title_token": "[visiting_liege.GetTitleAsNameNoTooltip|U]",
        "possessive_pair": ("minha", "meu"),
        "title_pair": ("a cacadora", "o cacador"),
        "confirmed_prefix": "meu [visiting_liege.gettitleasnamenotooltip|u]",
        "english_markers": ("my ", "the hunter"),
    },
}
EP3_TRAVEL_TITLE_ADJECTIVE_PATTERNS = {
    "ep3_travel_events.3001.islamic_arabic": {
        "relative_path": "dlc/ep3/ep3_travel_events_3_l_spanish.yml",
        "required_missing_tokens": (
            "[Glossary( 'al-thughūr', 'AL_THUGHUR_GLOSS')]",
            "[lords_liege.Custom('ES_ElLa')]",
            "[lords_liege.GetPrimaryTitle.GetNameNoTooltip]",
            "[lords_liege.GetTitleAsName|<STYLE>]",
        ),
        "required_extra_tokens": (
            "[Glossary( 'al-thughūr', 'AL_THUGHUR_GLOSS' )]",
            "[lords_liege.GetPrimaryTitle.GetAdjectiveNoTooltip]",
            "[lords_liege.GetTitleAsName]",
        ),
        "required_corrected_tokens": (
            "[lords_liege.GetTitleAsName]",
            "[lords_liege.GetPrimaryTitle.GetAdjectiveNoTooltip]",
            "[Glossary( 'al-thughūr', 'AL_THUGHUR_GLOSS' )]",
        ),
        "required_confirmed_tokens": (
            "[lords_liege.Custom('ES_ElLa')]",
            "[lords_liege.GetTitleAsName|l]",
            "[lords_liege.GetPrimaryTitle.GetNameNoTooltip]",
        ),
        "required_corrected_markers": (
            "portas cilicias",
            "al-thughur",
            "califado rashidun",
            "isl",
        ),
        "english_markers": (
            "cilician gates",
            "al-thughur",
            "[lords_liege.getprimarytitle.getadjectivenotooltip] [lords_liege.gettitleasname]",
            "rashidun caliphate",
        ),
    },
}


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def latest_bridge_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_boundary_token_policy_bridge_runs
        WHERE finished_at IS NOT NULL
          AND total_candidates > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete boundary token-policy bridge run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any], *, subpolicy_name: str) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_{subpolicy_name}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return [str(payload)]


def blocking_validation_issues(text: str | None) -> list[dict[str, Any]]:
    validation = local_quality_validator.validate_text(text)
    issues = validation.get("issues") or []
    blocked_codes = {
        "spanish_punctuation",
        "mojibake_or_unexpected_script",
        "utf8_mojibake_sequence",
        "replacement_question_mark_mojibake",
        "spanish_residue",
        "spanish_residue_in_literal",
        "gender_token_extra_suffix",
    }
    return [
        issue
        for issue in issues
        if issue.get("severity") == "high" or issue.get("code") in blocked_codes
    ]


def simplify_style_token(token: str) -> str:
    if token.startswith("[") and token.endswith("]") and "|<STYLE>" in token:
        return token.replace("|<STYLE>", "")
    return token


def non_select_style_equivalent(missing_tokens: list[str], extra_tokens: list[str]) -> bool:
    missing = [token for token in missing_tokens if not token.startswith("[Select_CString(")]
    extra = [token for token in extra_tokens if not token.startswith("[Select_CString(")]
    return Counter(simplify_style_token(token) for token in missing) == Counter(
        simplify_style_token(token) for token in extra
    )


def has_select_token(tokens: list[str]) -> bool:
    return any(token.startswith("[Select_CString(") for token in tokens)


def corrected_has_invariant_ptbr_verb(corrected_text: str | None) -> tuple[bool, list[str]]:
    lowered = (corrected_text or "").lower()
    matched = [verb for verb in sorted(PTBR_INVARIANT_VERBS) if re.search(rf"\b{re.escape(verb)}\b", lowered)]
    return bool(matched), matched


def select_cstring_literal_pairs(text: str | None) -> list[tuple[str, str]]:
    if not text:
        return []
    return [(left, right) for left, right in SELECT_CSTRING_LITERAL_RE.findall(text)]


def normalize_literal_key(value: str | None) -> str:
    lowered = (value or "").lower()
    try:
        lowered = lowered.encode("latin1").decode("utf-8")
    except UnicodeError:
        pass
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", lowered)
        if not unicodedata.combining(char)
    )


def select_cstring_localplayer_literal_refs(text: str | None) -> list[dict[str, str]]:
    if not text:
        return []
    refs: list[dict[str, str]] = []
    for scope, left, right in SELECT_CSTRING_LOCALPLAYER_LITERAL_RE.findall(text):
        refs.append(
            {
                "scope": scope,
                "left_literal": left,
                "right_literal": right,
                "normalized_left": normalize_literal_key(left),
                "normalized_right": normalize_literal_key(right),
            }
        )
    return refs


def select_cstring_direct_name_refs(text: str | None) -> list[dict[str, str]]:
    if not text:
        return []
    refs: list[dict[str, str]] = []
    for select_scope, local_literal, name_scope in SELECT_CSTRING_DIRECT_NAME_RE.findall(text):
        refs.append(
            {
                "select_scope": select_scope,
                "local_literal": local_literal,
                "name_scope": name_scope,
            }
        )
    return refs


def direct_short_ui_name_scope(token: str) -> str:
    match = DIRECT_SHORT_UI_TOKEN_RE.match(token)
    return match.group(1) if match else ""


def glossary_literal_pairs(text: str | None) -> list[tuple[str, str]]:
    if not text:
        return []
    return [(label, glossary_id) for label, glossary_id in GLOSSARY_LITERAL_RE.findall(text)]


def glossary_visible_label_pairs(missing_tokens: list[str], extra_tokens: list[str]) -> list[dict[str, str]]:
    if len(missing_tokens) != 1 or len(extra_tokens) != 1:
        return []
    missing_pairs = glossary_literal_pairs(missing_tokens[0])
    extra_pairs = glossary_literal_pairs(extra_tokens[0])
    if len(missing_pairs) != 1 or len(extra_pairs) != 1:
        return []
    old_label, old_glossary_id = missing_pairs[0]
    new_label, new_glossary_id = extra_pairs[0]
    return [
        {
            "old_label": old_label,
            "new_label": new_label,
            "old_glossary_id": old_glossary_id,
            "new_glossary_id": new_glossary_id,
        }
    ]


def glossary_ids_preserved(pairs: list[dict[str, str]]) -> bool:
    return bool(pairs) and all(pair["old_glossary_id"] == pair["new_glossary_id"] for pair in pairs)


def glossary_labels_changed(pairs: list[dict[str, str]]) -> bool:
    return bool(pairs) and all(pair["old_label"] != pair["new_label"] for pair in pairs)


def es_deldela_helper_scope(token: str) -> str:
    match = ES_DELDELA_HELPER_RE.match(token)
    return match.group(1) if match else ""


def has_dynamic_scope_token(text: str | None, scope: str) -> bool:
    if not text or not scope:
        return False
    return bool(re.search(rf"\[{re.escape(scope)}\.", text))


def has_literal_de_before_scope(text: str | None, scope: str) -> bool:
    if not text or not scope:
        return False
    return bool(re.search(rf"\bde\s+\[{re.escape(scope)}\.", text, flags=re.IGNORECASE))


def has_short_ui_name(text: str | None, scope: str, *, uppercase: bool) -> bool:
    if not text or not scope:
        return False
    style = r"\|U" if uppercase else r"(?:\|U)?"
    return bool(re.search(rf"\[{re.escape(scope)}\.GetShortUIName{style}\]", text))


def literal_word_present(text: str | None, literal: str) -> bool:
    if not text or not literal:
        return False
    return bool(re.search(rf"\b{re.escape(literal)}\b", text, flags=re.IGNORECASE))


def es_helper_infos(tokens: list[str]) -> list[dict[str, str]]:
    infos: list[dict[str, str]] = []
    for token in tokens:
        match = ES_HELPER_TOKEN_RE.match(token)
        if not match:
            continue
        infos.append(
            {
                "token": token,
                "scope": match.group(1),
                "helper": f"ES_{match.group(2)}",
            }
        )
    return infos


def es_helper_markers(text: str | None) -> list[str]:
    if not text:
        return []
    return ES_HELPER_IN_TEXT_RE.findall(text)


def allowed_es_narrative_nonhelper_missing(token: str) -> bool:
    if token.startswith("[Select_CString(") or "LocalPlayerString(" in token:
        return False
    return bool(ALLOWED_ES_NARRATIVE_NONHELPER_RE.match(token))


def allowed_hostage_context_nonhelper_missing(token: str) -> bool:
    if token in {"\\n", "\n", "\\r\\n", "\r\n"}:
        return True
    if token.startswith("[Select_CString(") or "LocalPlayerString(" in token:
        return False
    if ALLOWED_ES_NARRATIVE_NONHELPER_RE.match(token):
        return True
    return "Custom2('RelationToMe" in token


def has_relation_token(tokens: list[str]) -> bool:
    return any("Custom2('RelationToMe" in token for token in tokens)


def has_family_neutralization(text: str | None) -> bool:
    lowered = (text or "").lower()
    return "família" in lowered or "familia" in lowered


def has_hostage_term(text: str | None) -> bool:
    lowered = (text or "").lower()
    return "refém" in lowered or "refem" in lowered


def fetch_rows(conn, *, bridge_run_id: int, subpolicy_name: str) -> list[dict[str, Any]]:
    if subpolicy_name not in SUBPOLICY_BUCKETS:
        raise ValueError(f"Unsupported token subpolicy: {subpolicy_name}")
    policy_buckets = SUBPOLICY_BUCKETS[subpolicy_name]
    if isinstance(policy_buckets, str):
        bucket_sql = "bridge.policy_bucket = ?"
        params: tuple[Any, ...] = (bridge_run_id, policy_buckets)
    else:
        placeholders = ",".join("?" for _ in policy_buckets)
        bucket_sql = f"bridge.policy_bucket IN ({placeholders})"
        params = (bridge_run_id, *policy_buckets)
    boundary_sql = ""
    if subpolicy_name == ES_HELPER_NARRATIVE_SUBPOLICY:
        boundary_sql = "AND bridge.boundary_policy = 'weak_auto_issue_es_helper_narrative'"
    elif subpolicy_name == HOSTAGE_CONTEXT_SUBPOLICY:
        boundary_sql = "AND bridge.boundary_policy = 'weak_auto_issue_hostage_context'"
    elif subpolicy_name == LOCALPLAYERSTRING_WAR_JOIN_SUBPOLICY:
        boundary_sql = """
          AND bridge.boundary_policy = 'weak_auto_issue_dynamic_spanish_literal'
          AND bridge.relative_path = 'wars_l_spanish.yml'
          AND bridge.source_key IN ('liege_will_join_war_message', 'liege_will_not_join_war_message')
        """
    elif subpolicy_name == SELECT_CSTRING_DIRECT_NAME_SUBPOLICY:
        boundary_sql = """
          AND bridge.boundary_policy = 'weak_auto_issue_dynamic_spanish_literal'
          AND bridge.missing_tokens_json LIKE '%Select_CString%'
          AND bridge.missing_tokens_json LIKE '%IsLocalPlayer%'
          AND bridge.missing_tokens_json LIKE '%GetShortUIName%'
        """
    elif subpolicy_name == SHORT_DYNAMIC_VERB_SUBPOLICY:
        boundary_sql = """
          AND bridge.boundary_policy = 'weak_auto_short_dynamic_spanish_verb'
          AND bridge.missing_tokens_json LIKE '%Select_CString%'
        """
    elif subpolicy_name == HUNT_ACTIVITY_SELECT_CSTRING_SUBPOLICY:
        boundary_sql = """
          AND bridge.boundary_policy = 'weak_auto_issue_dynamic_spanish_literal'
          AND bridge.relative_path = 'activities/hunt_activity_l_spanish.yml'
          AND bridge.source_key IN ('hunt_activity_chase_tt', 'hunt_activity_captive_tt')
          AND bridge.missing_tokens_json LIKE '%Select_CString%'
        """
    elif subpolicy_name == CORONATION_TITLE_ES_HELPER_SUBPOLICY:
        boundary_sql = """
          AND bridge.boundary_policy = 'weak_auto_issue_inline_spanish_literal'
          AND bridge.relative_path = 'dlc/ach/dlc_ach_guest_intent_coronation_events_l_spanish.yml'
          AND bridge.source_key = 'guest_intent_coronation_events.0100.desc.magnificence_loss_subtle'
          AND bridge.missing_tokens_json LIKE '%ES_AlAla%'
          AND bridge.missing_tokens_json LIKE '%ES_ElLa%'
          AND bridge.missing_tokens_json LIKE '%ES_OA%'
        """
    elif subpolicy_name == NICKNAME_WHISPERER_SELECT_CSTRING_SUBPOLICY:
        boundary_sql = """
          AND bridge.boundary_policy = 'weak_auto_issue_dynamic_spanish_literal'
          AND bridge.relative_path = 'nicknames_l_spanish.yml'
          AND bridge.source_key = 'nick_the_whisperer_desc'
          AND bridge.missing_tokens_json LIKE '%ES_OA%'
          AND bridge.missing_tokens_json LIKE '%Select_CString%'
        """
    elif subpolicy_name == SINGLE_COMBAT_VICTOR_NAME_SUBPOLICY:
        boundary_sql = """
          AND bridge.boundary_policy = 'weak_auto_residual_spanish_literal_repair'
          AND bridge.relative_path = 'single_combat_events_l_spanish.yml'
          AND bridge.source_key = 'single_combat.0031.desc.sc_attacker.mocking_boast'
          AND bridge.extra_tokens_json LIKE '%sc_victor.GetFirstNameNoTooltip%'
        """
    elif subpolicy_name == TOUR_TITLE_POSSESSIVE_SUBPOLICY:
        boundary_sql = """
          AND bridge.boundary_policy = 'weak_auto_dynamic_select_cstring_spanish_literal'
          AND bridge.relative_path = 'event_localization/activities/tour_events_l_spanish.yml'
          AND bridge.source_key = 'tour_grounds_events.3004.title'
          AND bridge.extra_tokens_json LIKE '%Select_CString%'
          AND bridge.extra_tokens_json LIKE '%visiting_liege.IsFemale%'
        """
    elif subpolicy_name == EP3_TRAVEL_TITLE_ADJECTIVE_SUBPOLICY:
        boundary_sql = """
          AND bridge.boundary_policy = 'weak_auto_residual_spanish_literal_repair'
          AND bridge.relative_path = 'dlc/ep3/ep3_travel_events_3_l_spanish.yml'
          AND bridge.source_key = 'ep3_travel_events.3001.islamic_arabic'
          AND bridge.missing_tokens_json LIKE '%ES_ElLa%'
          AND bridge.extra_tokens_json LIKE '%GetPrimaryTitle.GetAdjectiveNoTooltip%'
        """
    rows = conn.execute(
        f"""
        SELECT
            bridge.id AS bridge_item_id,
            bridge.run_id AS bridge_run_id,
            bridge.repair_queue_item_id,
            bridge.boundary_policy_item_id,
            bridge.review_decision_id,
            bridge.segment_id,
            bridge.relative_path,
            bridge.source_key,
            bridge.source_line_number,
            bridge.boundary_agent_key,
            bridge.boundary_policy,
            bridge.policy_bucket,
            bridge.risk_level,
            bridge.missing_tokens_json,
            bridge.extra_tokens_json,
            bridge.issue_flags_json,
            bridge.current_confirmed_text_hash,
            bridge.corrected_text_hash,
            decision.decision,
            decision.evidence_label,
            decision.corrected_text,
            decision.notes,
            confirmation.confirmed_text,
            source.english_text,
            source.spanish_text,
            source.old_text
        FROM auto_confirmation_reopen_text_boundary_token_policy_bridge_items bridge
        JOIN auto_confirmation_reopen_text_review_decisions decision
          ON decision.id = bridge.review_decision_id
        JOIN source_segments source ON source.id = bridge.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = bridge.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        WHERE bridge.run_id = ?
          AND {bucket_sql}
          {boundary_sql}
        ORDER BY bridge.id
        """,
        params,
    ).fetchall()
    return [evaluate_row(dict(row), subpolicy_name=subpolicy_name) for row in rows]


def evaluate_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    if subpolicy_name == SELECT_CSTRING_SUBPOLICY:
        return evaluate_select_cstring_row(row, subpolicy_name=subpolicy_name)
    if subpolicy_name == GLOSSARY_LABEL_SUBPOLICY:
        return evaluate_glossary_label_row(row, subpolicy_name=subpolicy_name)
    if subpolicy_name == ES_DELDELA_LITERAL_DE_SUBPOLICY:
        return evaluate_es_deldela_literal_de_row(row, subpolicy_name=subpolicy_name)
    if subpolicy_name == DYNAMIC_SCOPE_HELPER_SUBPOLICY:
        return evaluate_dynamic_scope_helper_row(row, subpolicy_name=subpolicy_name)
    if subpolicy_name == SELECT_CSTRING_DIRECT_NAME_SUBPOLICY:
        return evaluate_select_cstring_direct_name_row(row, subpolicy_name=subpolicy_name)
    if subpolicy_name == LOCALPLAYERSTRING_WAR_JOIN_SUBPOLICY:
        return evaluate_localplayerstring_war_join_row(row, subpolicy_name=subpolicy_name)
    if subpolicy_name == ES_HELPER_NARRATIVE_SUBPOLICY:
        return evaluate_es_helper_narrative_row(row, subpolicy_name=subpolicy_name)
    if subpolicy_name == HOSTAGE_CONTEXT_SUBPOLICY:
        return evaluate_hostage_context_row(row, subpolicy_name=subpolicy_name)
    if subpolicy_name == SHORT_DYNAMIC_VERB_SUBPOLICY:
        return evaluate_short_dynamic_verb_row(row, subpolicy_name=subpolicy_name)
    if subpolicy_name == HUNT_ACTIVITY_SELECT_CSTRING_SUBPOLICY:
        return evaluate_hunt_activity_select_cstring_row(row, subpolicy_name=subpolicy_name)
    if subpolicy_name == CORONATION_TITLE_ES_HELPER_SUBPOLICY:
        return evaluate_coronation_title_es_helper_row(row, subpolicy_name=subpolicy_name)
    if subpolicy_name == NICKNAME_WHISPERER_SELECT_CSTRING_SUBPOLICY:
        return evaluate_nickname_whisperer_select_cstring_row(row, subpolicy_name=subpolicy_name)
    if subpolicy_name == SINGLE_COMBAT_VICTOR_NAME_SUBPOLICY:
        return evaluate_single_combat_victor_name_row(row, subpolicy_name=subpolicy_name)
    if subpolicy_name == TOUR_TITLE_POSSESSIVE_SUBPOLICY:
        return evaluate_tour_title_possessive_row(row, subpolicy_name=subpolicy_name)
    if subpolicy_name == EP3_TRAVEL_TITLE_ADJECTIVE_SUBPOLICY:
        return evaluate_ep3_travel_title_adjective_row(row, subpolicy_name=subpolicy_name)
    raise ValueError(f"Unsupported token subpolicy: {subpolicy_name}")


def evaluate_select_cstring_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    has_select = has_select_token(missing_tokens)
    corrected_contains_select = "[Select_CString(" in (row.get("corrected_text") or "")
    has_ptbr_verb, matched_verbs = corrected_has_invariant_ptbr_verb(row.get("corrected_text"))
    style_ok = non_select_style_equivalent(missing_tokens, extra_tokens)
    literal_pairs = select_cstring_literal_pairs(row.get("confirmed_text"))

    blockers: list[str] = []
    if not has_select:
        blockers.append("no_missing_select_cstring_token")
    if corrected_contains_select:
        blockers.append("corrected_text_still_has_select_cstring")
    if not has_ptbr_verb:
        blockers.append("no_known_invariant_ptbr_verb")
    if not style_ok:
        blockers.append("non_select_structural_change")
    if validation_issues:
        blockers.append("validation_issue")
    if not literal_pairs:
        blockers.append("missing_select_cstring_literal_pairs")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_select_cstring_invariant_ptbr_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "select_cstring_literal_pairs": literal_pairs,
        "matched_ptbr_verbs": matched_verbs,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "style_equivalent_non_select_tokens": style_ok,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": row.get("current_confirmed_text_hash") or sha256_text(row.get("confirmed_text")),
        "corrected_text_hash": row.get("corrected_text_hash") or sha256_text(row.get("corrected_text")),
    }


def evaluate_glossary_label_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    pairs = glossary_visible_label_pairs(missing_tokens, extra_tokens)
    confirmed_pairs = glossary_literal_pairs(row.get("confirmed_text"))
    corrected_pairs = glossary_literal_pairs(row.get("corrected_text"))
    english_pairs = glossary_literal_pairs(row.get("english_text"))
    spanish_pairs = glossary_literal_pairs(row.get("spanish_text"))
    old_pairs = glossary_literal_pairs(row.get("old_text"))
    ids_ok = glossary_ids_preserved(pairs)
    labels_changed = glossary_labels_changed(pairs)
    only_glossary_delta = len(pairs) == 1
    corrected_has_expected_label = bool(
        pairs and pairs[0]["new_label"] in (row.get("corrected_text") or "")
    )
    confirmed_has_old_label = bool(
        pairs and pairs[0]["old_label"] in (row.get("confirmed_text") or "")
    )
    boundary_policy_ok = row.get("boundary_policy") == "weak_auto_embedded_glossary_visible_label"

    blockers: list[str] = []
    if not boundary_policy_ok:
        blockers.append("wrong_boundary_policy")
    if not only_glossary_delta:
        blockers.append("not_single_glossary_label_delta")
    if not ids_ok:
        blockers.append("glossary_id_changed")
    if not labels_changed:
        blockers.append("visible_label_not_changed")
    if not corrected_has_expected_label:
        blockers.append("corrected_label_not_in_corrected_text")
    if not confirmed_has_old_label:
        blockers.append("old_label_not_in_confirmed_text")
    if validation_issues:
        blockers.append("validation_issue")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_glossary_visible_label_ptbr_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "glossary_visible_label_pairs": pairs,
        "confirmed_glossary_pairs": confirmed_pairs,
        "corrected_glossary_pairs": corrected_pairs,
        "english_glossary_pairs": english_pairs,
        "spanish_glossary_pairs": spanish_pairs,
        "old_glossary_pairs": old_pairs,
        "same_glossary_ids": ids_ok,
        "labels_changed": labels_changed,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": row.get("current_confirmed_text_hash") or sha256_text(row.get("confirmed_text")),
        "corrected_text_hash": row.get("corrected_text_hash") or sha256_text(row.get("corrected_text")),
    }


def evaluate_es_deldela_literal_de_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    helper_scope = es_deldela_helper_scope(missing_tokens[0]) if len(missing_tokens) == 1 else ""
    confirmed_text = row.get("confirmed_text") or ""
    corrected_text = row.get("corrected_text") or ""
    expected_helper_present = bool(missing_tokens and missing_tokens[0] in confirmed_text)
    corrected_has_helper = "ES_DelDela" in corrected_text
    confirmed_has_scope = has_dynamic_scope_token(confirmed_text, helper_scope)
    corrected_has_scope = has_dynamic_scope_token(corrected_text, helper_scope)
    connector_ok = has_literal_de_before_scope(corrected_text, helper_scope)
    allowed_boundary_policies = {
        "weak_auto_custom_loc_es_helper",
        "weak_auto_visible_possessive_connector_loss",
        "weak_auto_visible_sentence_collapse",
    }

    blockers: list[str] = []
    if row.get("boundary_policy") not in allowed_boundary_policies:
        blockers.append("wrong_boundary_policy")
    if len(missing_tokens) != 1:
        blockers.append("not_single_missing_token")
    if extra_tokens:
        blockers.append("unexpected_extra_tokens")
    if not helper_scope:
        blockers.append("missing_es_deldela_helper")
    if not expected_helper_present:
        blockers.append("helper_not_in_confirmed_text")
    if corrected_has_helper:
        blockers.append("corrected_still_has_es_deldela")
    if not confirmed_has_scope:
        blockers.append("confirmed_missing_dynamic_scope")
    if not corrected_has_scope:
        blockers.append("corrected_missing_dynamic_scope")
    if not connector_ok:
        blockers.append("missing_literal_de_before_scope")
    if validation_issues:
        blockers.append("validation_issue")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_es_deldela_literal_de_ptbr_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "helper": "ES_DelDela",
        "helper_scope": helper_scope,
        "expected_helper_present": expected_helper_present,
        "corrected_has_helper": corrected_has_helper,
        "confirmed_has_dynamic_scope": confirmed_has_scope,
        "corrected_has_dynamic_scope": corrected_has_scope,
        "literal_de_before_scope": connector_ok,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": row.get("current_confirmed_text_hash") or sha256_text(row.get("confirmed_text")),
        "corrected_text_hash": row.get("corrected_text_hash") or sha256_text(row.get("corrected_text")),
    }


def evaluate_dynamic_scope_helper_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    confirmed_text = row.get("confirmed_text") or ""
    corrected_text = row.get("corrected_text") or ""
    helper_mode = ""
    helper_scope = ""
    helper_literal = ""
    dynamic_scope_preserved = False
    corrected_has_spanish_helper = "ES_only_el_GetShortUIName" in corrected_text or "LocalPlayerString" in corrected_text

    article_match = ES_ONLY_EL_SHORT_UI_RE.search(confirmed_text)
    local_player_match = LOCAL_PLAYER_STRING_RE.search(confirmed_text)
    if article_match:
        helper_mode = "es_article_helper_plain_name"
        helper_scope = article_match.group(1)
        helper_literal = "ES_only_el_GetShortUIName"
        uppercase = bool(article_match.group(2))
        dynamic_scope_preserved = has_short_ui_name(corrected_text, helper_scope, uppercase=uppercase)
    elif local_player_match and local_player_match.group(2) == local_player_match.group(3):
        helper_mode = "localplayerstring_invariant_literal"
        helper_scope = local_player_match.group(1)
        helper_literal = local_player_match.group(2)
        dynamic_scope_preserved = (
            has_short_ui_name(corrected_text, helper_scope, uppercase=True)
            and literal_word_present(corrected_text, helper_literal)
        )

    blockers: list[str] = []
    if row.get("boundary_policy") not in {
        "weak_auto_custom_loc_es_helper",
        "weak_auto_visible_copula_token_form",
    }:
        blockers.append("wrong_boundary_policy")
    if not helper_mode:
        blockers.append("unsupported_dynamic_scope_pattern")
    if corrected_has_spanish_helper:
        blockers.append("corrected_still_has_spanish_dynamic_helper")
    if not dynamic_scope_preserved:
        blockers.append("dynamic_scope_not_preserved")
    if helper_mode == "es_article_helper_plain_name" and "Custom(" in corrected_text:
        blockers.append("corrected_still_has_custom_helper")
    if helper_mode == "localplayerstring_invariant_literal" and helper_literal != "é":
        blockers.append("non_ptbr_invariant_localplayer_literal")
    if validation_issues:
        blockers.append("validation_issue")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_dynamic_scope_ptbr_helper_neutralization_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "helper_mode": helper_mode,
        "helper_scope": helper_scope,
        "helper_literal": helper_literal,
        "corrected_has_spanish_helper": corrected_has_spanish_helper,
        "dynamic_scope_preserved": dynamic_scope_preserved,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": row.get("current_confirmed_text_hash") or sha256_text(row.get("confirmed_text")),
        "corrected_text_hash": row.get("corrected_text_hash") or sha256_text(row.get("corrected_text")),
    }


def evaluate_select_cstring_direct_name_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    confirmed_text = row.get("confirmed_text") or ""
    corrected_text = row.get("corrected_text") or ""
    english_text = row.get("english_text") or ""
    direct_refs = select_cstring_direct_name_refs(confirmed_text)
    extra_scope = direct_short_ui_name_scope(extra_tokens[0]) if len(extra_tokens) == 1 else ""
    direct_name_token = f"[{extra_scope}.GetShortUIName]" if extra_scope else ""
    matching_ref = next(
        (
            ref
            for ref in direct_refs
            if ref["select_scope"] == extra_scope and ref["name_scope"] == extra_scope
        ),
        None,
    )
    boundary_policy_ok = row.get("boundary_policy") == "weak_auto_issue_dynamic_spanish_literal"
    missing_select = len(missing_tokens) == 1 and missing_tokens[0].startswith("[Select_CString(")
    corrected_has_direct_name = bool(direct_name_token and direct_name_token in corrected_text)
    english_has_direct_name = bool(direct_name_token and direct_name_token in english_text)
    corrected_has_select = "Select_CString(" in corrected_text

    blockers: list[str] = []
    if not boundary_policy_ok:
        blockers.append("wrong_boundary_policy")
    if not missing_select:
        blockers.append("not_single_missing_select_cstring")
    if len(extra_tokens) != 1 or not extra_scope:
        blockers.append("not_single_direct_short_ui_extra")
    if not matching_ref:
        blockers.append("confirmed_select_cstring_not_direct_name_reference")
    if corrected_has_select:
        blockers.append("corrected_text_still_has_select_cstring")
    if not corrected_has_direct_name:
        blockers.append("corrected_missing_direct_name_reference")
    if not english_has_direct_name:
        blockers.append("english_missing_direct_name_reference")
    if validation_issues:
        blockers.append("validation_issue")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_select_cstring_direct_name_reference_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "select_cstring_direct_name_reference": matching_ref is not None,
        "select_scope": matching_ref["select_scope"] if matching_ref else "",
        "direct_name_scope": extra_scope,
        "local_literal": matching_ref["local_literal"] if matching_ref else "",
        "direct_name_token": direct_name_token,
        "corrected_has_direct_name": corrected_has_direct_name,
        "english_has_direct_name": english_has_direct_name,
        "corrected_has_select_cstring": corrected_has_select,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": row.get("current_confirmed_text_hash") or sha256_text(row.get("confirmed_text")),
        "corrected_text_hash": row.get("corrected_text_hash") or sha256_text(row.get("corrected_text")),
    }


def evaluate_localplayerstring_war_join_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    confirmed_text = row.get("confirmed_text") or ""
    corrected_text = row.get("corrected_text") or ""
    local_player_match = LOCAL_PLAYER_STRING_RE.search(confirmed_text)
    source_key = row.get("source_key") or ""
    war_join_state = ""
    expected_phrase = ""
    if source_key == "liege_will_join_war_message":
        war_join_state = "join"
        expected_phrase = "#bold se juntar\u00e1#!"
    elif source_key == "liege_will_not_join_war_message":
        war_join_state = "not_join"
        expected_phrase = "#bold n\u00e3o se juntar\u00e1#!"

    boundary_policy_ok = row.get("boundary_policy") == "weak_auto_issue_dynamic_spanish_literal"
    missing_localplayer = len(missing_tokens) == 1 and missing_tokens[0].startswith("[Character.LocalPlayerString(")
    corrected_has_character_name = "[Character.GetShortUIName]" in corrected_text
    corrected_has_localplayerstring = "LocalPlayerString(" in corrected_text
    corrected_phrase_ok = bool(expected_phrase and expected_phrase in corrected_text and "\u00e0 guerra" in corrected_text)
    interaction_token_preserved = "[DeclareWarInteractionWindow.GetProtectVassalInteractionValue|-]" in corrected_text

    blockers: list[str] = []
    if not boundary_policy_ok:
        blockers.append("wrong_boundary_policy")
    if row.get("relative_path") != "wars_l_spanish.yml":
        blockers.append("wrong_relative_path")
    if not war_join_state:
        blockers.append("unsupported_war_join_key")
    if not missing_localplayer:
        blockers.append("not_single_missing_character_localplayerstring")
    if extra_tokens:
        blockers.append("unexpected_extra_tokens")
    if not local_player_match:
        blockers.append("confirmed_missing_character_localplayerstring")
    if corrected_has_localplayerstring:
        blockers.append("corrected_still_has_localplayerstring")
    if not corrected_has_character_name:
        blockers.append("corrected_missing_character_name")
    if not corrected_phrase_ok:
        blockers.append("corrected_missing_expected_war_join_phrase")
    if not interaction_token_preserved:
        blockers.append("interaction_value_token_not_preserved")
    if validation_issues:
        blockers.append("validation_issue")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_localplayerstring_war_join_neutralization_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "helper_mode": "localplayerstring_war_join_message" if war_join_state else "",
        "helper_scope": local_player_match.group(1) if local_player_match else "",
        "helper_literals": list(local_player_match.groups()[1:]) if local_player_match else [],
        "war_join_state": war_join_state,
        "dynamic_scope_removed": not corrected_has_localplayerstring,
        "corrected_has_character_name": corrected_has_character_name,
        "corrected_phrase_ok": corrected_phrase_ok,
        "interaction_token_preserved": interaction_token_preserved,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": row.get("current_confirmed_text_hash") or sha256_text(row.get("confirmed_text")),
        "corrected_text_hash": row.get("corrected_text_hash") or sha256_text(row.get("corrected_text")),
    }


def evaluate_es_helper_narrative_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    confirmed_text = row.get("confirmed_text") or ""
    corrected_text = row.get("corrected_text") or ""
    helper_infos = es_helper_infos(missing_tokens)
    helper_tokens = {info["token"] for info in helper_infos}
    helper_scopes = sorted({info["scope"] for info in helper_infos})
    helper_kinds = sorted({info["helper"] for info in helper_infos})
    confirmed_helper_markers = es_helper_markers(confirmed_text)
    corrected_helper_markers = es_helper_markers(corrected_text)
    extra_helper_infos = es_helper_infos(extra_tokens)
    unsupported_nonhelper_missing = [
        token
        for token in missing_tokens
        if token not in helper_tokens and not allowed_es_narrative_nonhelper_missing(token)
    ]
    preserved_helper_scopes = [
        scope for scope in helper_scopes if has_dynamic_scope_token(corrected_text, scope)
    ]
    neutralized_helper_scopes = [
        scope for scope in helper_scopes if scope not in preserved_helper_scopes
    ]
    direct_name_extra_scopes = [
        scope for scope in (direct_short_ui_name_scope(token) for token in extra_tokens) if scope
    ]
    corrected_text_hash = row.get("corrected_text_hash") or sha256_text(corrected_text)
    confirmed_text_hash = row.get("current_confirmed_text_hash") or sha256_text(confirmed_text)
    has_text_delta = corrected_text_hash != confirmed_text_hash
    confirmed_contains_missing_helpers = all(info["token"] in confirmed_text for info in helper_infos)
    corrected_removed_missing_helpers = all(info["token"] not in corrected_text for info in helper_infos)

    blockers: list[str] = []
    if row.get("boundary_policy") != "weak_auto_issue_es_helper_narrative":
        blockers.append("wrong_boundary_policy")
    if row.get("policy_bucket") not in ES_HELPER_NARRATIVE_BUCKETS:
        blockers.append("wrong_policy_bucket")
    if not helper_infos:
        blockers.append("missing_es_helper_evidence")
    if not confirmed_helper_markers:
        blockers.append("confirmed_missing_es_helper_marker")
    if not confirmed_contains_missing_helpers:
        blockers.append("missing_helper_not_present_in_confirmed_text")
    if corrected_helper_markers:
        blockers.append("corrected_still_has_es_helper")
    if extra_helper_infos:
        blockers.append("extra_token_adds_es_helper")
    if not corrected_removed_missing_helpers:
        blockers.append("corrected_did_not_remove_all_missing_helpers")
    if unsupported_nonhelper_missing:
        blockers.append("unsupported_nonhelper_missing_token")
    if not has_text_delta:
        blockers.append("no_text_delta")
    if not corrected_text:
        blockers.append("missing_corrected_text")
    if validation_issues:
        blockers.append("validation_issue")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_es_helper_narrative_neutralization_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "decision": row.get("decision"),
        "evidence_label": row.get("evidence_label"),
        "helper_infos": helper_infos,
        "helper_scopes": helper_scopes,
        "helper_kinds": helper_kinds,
        "confirmed_helper_count": len(confirmed_helper_markers),
        "corrected_helper_count": len(corrected_helper_markers),
        "confirmed_contains_missing_helpers": confirmed_contains_missing_helpers,
        "corrected_removed_missing_helpers": corrected_removed_missing_helpers,
        "preserved_helper_scopes": preserved_helper_scopes,
        "neutralized_helper_scopes": neutralized_helper_scopes,
        "direct_name_extra_scopes": direct_name_extra_scopes,
        "unsupported_nonhelper_missing": unsupported_nonhelper_missing,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": confirmed_text_hash,
        "corrected_text_hash": corrected_text_hash,
    }


def evaluate_hostage_context_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    confirmed_text = row.get("confirmed_text") or ""
    corrected_text = row.get("corrected_text") or ""
    helper_infos = es_helper_infos(missing_tokens)
    helper_tokens = {info["token"] for info in helper_infos}
    helper_scopes = sorted({info["scope"] for info in helper_infos})
    helper_kinds = sorted({info["helper"] for info in helper_infos})
    confirmed_helper_markers = es_helper_markers(confirmed_text)
    corrected_helper_markers = es_helper_markers(corrected_text)
    extra_helper_infos = es_helper_infos(extra_tokens)
    unsupported_nonhelper_missing = [
        token
        for token in missing_tokens
        if token not in helper_tokens and not allowed_hostage_context_nonhelper_missing(token)
    ]
    relation_removed = has_relation_token(missing_tokens) and not has_relation_token(extra_tokens)
    relation_preserved_in_corrected = "Custom2('RelationToMe" in corrected_text
    family_neutralization = has_family_neutralization(corrected_text)
    corrected_has_hostage = has_hostage_term(corrected_text)
    direct_name_extra_scopes = [
        scope for scope in (direct_short_ui_name_scope(token) for token in extra_tokens) if scope
    ]
    corrected_text_hash = row.get("corrected_text_hash") or sha256_text(corrected_text)
    confirmed_text_hash = row.get("current_confirmed_text_hash") or sha256_text(confirmed_text)
    has_text_delta = corrected_text_hash != confirmed_text_hash
    confirmed_contains_missing_helpers = all(info["token"] in confirmed_text for info in helper_infos)
    corrected_removed_missing_helpers = all(info["token"] not in corrected_text for info in helper_infos)

    blockers: list[str] = []
    if row.get("boundary_policy") != "weak_auto_issue_hostage_context":
        blockers.append("wrong_boundary_policy")
    if row.get("policy_bucket") not in HOSTAGE_CONTEXT_BUCKETS:
        blockers.append("wrong_policy_bucket")
    if not helper_infos:
        blockers.append("missing_es_helper_evidence")
    if not confirmed_helper_markers:
        blockers.append("confirmed_missing_es_helper_marker")
    if not confirmed_contains_missing_helpers:
        blockers.append("missing_helper_not_present_in_confirmed_text")
    if corrected_helper_markers:
        blockers.append("corrected_still_has_es_helper")
    if extra_helper_infos:
        blockers.append("extra_token_adds_es_helper")
    if not corrected_removed_missing_helpers:
        blockers.append("corrected_did_not_remove_all_missing_helpers")
    if unsupported_nonhelper_missing:
        blockers.append("unsupported_nonhelper_missing_token")
    if not corrected_has_hostage:
        blockers.append("corrected_missing_hostage_term")
    if relation_removed and not (relation_preserved_in_corrected or family_neutralization):
        blockers.append("relation_removed_without_neutral_family_phrase")
    if not has_text_delta:
        blockers.append("no_text_delta")
    if not corrected_text:
        blockers.append("missing_corrected_text")
    if validation_issues:
        blockers.append("validation_issue")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_hostage_context_neutralization_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "decision": row.get("decision"),
        "evidence_label": row.get("evidence_label"),
        "helper_infos": helper_infos,
        "helper_scopes": helper_scopes,
        "helper_kinds": helper_kinds,
        "confirmed_helper_count": len(confirmed_helper_markers),
        "corrected_helper_count": len(corrected_helper_markers),
        "confirmed_contains_missing_helpers": confirmed_contains_missing_helpers,
        "corrected_removed_missing_helpers": corrected_removed_missing_helpers,
        "relation_removed": relation_removed,
        "relation_preserved_in_corrected": relation_preserved_in_corrected,
        "family_neutralization": family_neutralization,
        "corrected_has_hostage_term": corrected_has_hostage,
        "direct_name_extra_scopes": direct_name_extra_scopes,
        "unsupported_nonhelper_missing": unsupported_nonhelper_missing,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": confirmed_text_hash,
        "corrected_text_hash": corrected_text_hash,
    }


def evaluate_short_dynamic_verb_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    confirmed_text = row.get("confirmed_text") or ""
    corrected_text = row.get("corrected_text") or ""
    english_text = row.get("english_text") or ""
    source_key = row.get("source_key") or ""
    pattern = SHORT_DYNAMIC_VERB_PATTERNS.get(source_key) or {}
    refs = select_cstring_localplayer_literal_refs(confirmed_text)
    english_refs = select_cstring_localplayer_literal_refs(english_text)
    normalized_expected = tuple(pattern.get("normalized_literals") or ())
    matched_ref = next(
        (
            ref
            for ref in refs
            if ref["scope"] == pattern.get("scope")
            and (ref["normalized_left"], ref["normalized_right"]) == normalized_expected
        ),
        None,
    )
    corrected_text_hash = row.get("corrected_text_hash") or sha256_text(corrected_text)
    confirmed_text_hash = row.get("current_confirmed_text_hash") or sha256_text(confirmed_text)
    corrected_normalized = normalize_literal_key(corrected_text)
    required_tokens = tuple(pattern.get("required_tokens") or ())
    required_phrases = tuple(pattern.get("required_phrases") or ())
    english_required_tokens = tuple(pattern.get("english_required_tokens") or ())
    english_alignment = pattern.get("english_alignment") or ""
    english_alignment_ok = False
    if english_alignment == "plain_required_tokens":
        english_alignment_ok = all(token in english_text for token in english_required_tokens)
    elif english_alignment == "parallel_select_cstring":
        english_alignment_ok = bool(
            any(ref["scope"] == pattern.get("scope") for ref in english_refs)
            and all(token in english_text for token in english_required_tokens)
        )

    boundary_policy_ok = row.get("boundary_policy") == "weak_auto_short_dynamic_spanish_verb"
    single_missing_select = len(missing_tokens) == 1 and missing_tokens[0].startswith("[Select_CString(")
    corrected_has_select = "Select_CString(" in corrected_text
    corrected_removed_spanish_literals = bool(
        normalized_expected and all(literal not in corrected_normalized for literal in normalized_expected)
    )
    required_tokens_preserved = bool(required_tokens and all(token in corrected_text for token in required_tokens))
    expected_phrase_present = bool(required_phrases and all(phrase in corrected_text for phrase in required_phrases))
    has_text_delta = corrected_text_hash != confirmed_text_hash
    allowed_source_key = bool(pattern and row.get("relative_path") == pattern.get("relative_path"))

    blockers: list[str] = []
    if not allowed_source_key:
        blockers.append("unsupported_short_dynamic_verb_key")
    if not boundary_policy_ok:
        blockers.append("wrong_boundary_policy")
    if row.get("policy_bucket") != SHORT_DYNAMIC_VERB_BUCKET:
        blockers.append("wrong_policy_bucket")
    if not single_missing_select:
        blockers.append("not_single_missing_select_cstring")
    if extra_tokens:
        blockers.append("unexpected_extra_tokens")
    if not matched_ref:
        blockers.append("confirmed_select_cstring_spanish_literals_not_matched")
    if corrected_has_select:
        blockers.append("corrected_text_still_has_select_cstring")
    if not corrected_removed_spanish_literals:
        blockers.append("corrected_still_has_spanish_dynamic_literals")
    if not required_tokens_preserved:
        blockers.append("required_tokens_not_preserved")
    if not expected_phrase_present:
        blockers.append("expected_ptbr_phrase_missing")
    if not english_alignment_ok:
        blockers.append("english_alignment_not_verified")
    if not has_text_delta:
        blockers.append("no_text_delta")
    if validation_issues:
        blockers.append("validation_issue")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_short_dynamic_spanish_verb_neutralization_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "helper_mode": "short_dynamic_spanish_verb_neutralization",
        "allowed_source_key": allowed_source_key,
        "matched_pattern_key": source_key if pattern else "",
        "select_scope": matched_ref["scope"] if matched_ref else "",
        "confirmed_select_cstring_spanish_literals": matched_ref is not None,
        "normalized_literal_pair": list(normalized_expected),
        "corrected_has_select_cstring": corrected_has_select,
        "corrected_removed_spanish_literals": corrected_removed_spanish_literals,
        "required_tokens_preserved": required_tokens_preserved,
        "expected_phrase_present": expected_phrase_present,
        "english_alignment": english_alignment,
        "english_alignment_ok": english_alignment_ok,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": confirmed_text_hash,
        "corrected_text_hash": corrected_text_hash,
    }


def evaluate_hunt_activity_select_cstring_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    confirmed_text = row.get("confirmed_text") or ""
    corrected_text = row.get("corrected_text") or ""
    english_text = row.get("english_text") or ""
    source_key = row.get("source_key") or ""
    pattern = HUNT_ACTIVITY_SELECT_CSTRING_PATTERNS.get(source_key) or {}
    refs = select_cstring_localplayer_literal_refs(confirmed_text)
    normalized_expected = tuple(pattern.get("normalized_literals") or ())
    matched_ref = next(
        (
            ref
            for ref in refs
            if ref["scope"] == pattern.get("scope")
            and (ref["normalized_left"], ref["normalized_right"]) == normalized_expected
        ),
        None,
    )
    corrected_text_hash = row.get("corrected_text_hash") or sha256_text(corrected_text)
    confirmed_text_hash = row.get("current_confirmed_text_hash") or sha256_text(confirmed_text)
    corrected_normalized = normalize_literal_key(corrected_text)
    confirmed_normalized = normalize_literal_key(confirmed_text)
    english_normalized = normalize_literal_key(english_text)
    required_tokens = tuple(pattern.get("required_tokens") or ())
    required_markers = tuple(pattern.get("required_markers") or ())
    english_markers = tuple(pattern.get("english_markers") or ())

    boundary_policy_ok = row.get("boundary_policy") == "weak_auto_issue_dynamic_spanish_literal"
    single_missing_select = len(missing_tokens) == 1 and missing_tokens[0].startswith("[Select_CString(")
    corrected_has_select = "Select_CString(" in corrected_text
    corrected_removed_redundant_select = bool(
        normalized_expected and all(literal in confirmed_normalized for literal in normalized_expected)
        and all(literal in corrected_normalized for literal in normalized_expected[:1])
    )
    removed_o_a_marker = "o/a [activity.custom('getanimaltype')]" in confirmed_normalized and (
        "o/a [activity.custom('getanimaltype')]" not in corrected_normalized
    )
    required_tokens_preserved = bool(required_tokens and all(token in corrected_text for token in required_tokens))
    required_markers_present = bool(
        required_markers
        and all(normalize_literal_key(marker) in corrected_normalized for marker in required_markers)
    )
    english_alignment_ok = bool(
        english_markers
        and all(normalize_literal_key(marker) in english_normalized for marker in english_markers)
    )
    has_text_delta = corrected_text_hash != confirmed_text_hash
    allowed_source_key = bool(pattern and row.get("relative_path") == pattern.get("relative_path"))

    blockers: list[str] = []
    if not allowed_source_key:
        blockers.append("unsupported_hunt_activity_key")
    if not boundary_policy_ok:
        blockers.append("wrong_boundary_policy")
    if row.get("policy_bucket") != HUNT_ACTIVITY_SELECT_CSTRING_BUCKET:
        blockers.append("wrong_policy_bucket")
    if not single_missing_select:
        blockers.append("not_single_missing_select_cstring")
    if extra_tokens:
        blockers.append("unexpected_extra_tokens")
    if not matched_ref:
        blockers.append("confirmed_select_cstring_redundant_literal_not_matched")
    if corrected_has_select:
        blockers.append("corrected_text_still_has_select_cstring")
    if not corrected_removed_redundant_select:
        blockers.append("corrected_missing_redundant_literal")
    if not removed_o_a_marker:
        blockers.append("animal_article_marker_not_neutralized")
    if not required_tokens_preserved:
        blockers.append("required_tokens_not_preserved")
    if not required_markers_present:
        blockers.append("required_hunt_markers_missing")
    if not english_alignment_ok:
        blockers.append("english_alignment_not_verified")
    if not has_text_delta:
        blockers.append("no_text_delta")
    if validation_issues:
        blockers.append("validation_issue")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_hunt_activity_select_cstring_neutralization_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "helper_mode": "hunt_activity_select_cstring_neutralization",
        "allowed_source_key": allowed_source_key,
        "matched_pattern_key": source_key if pattern else "",
        "select_scope": matched_ref["scope"] if matched_ref else "",
        "redundant_literal_pair": list(normalized_expected),
        "confirmed_select_cstring_redundant_literal": matched_ref is not None,
        "corrected_has_select_cstring": corrected_has_select,
        "corrected_removed_redundant_select": corrected_removed_redundant_select,
        "animal_article_marker_neutralized": removed_o_a_marker,
        "required_tokens_preserved": required_tokens_preserved,
        "required_hunt_markers_present": required_markers_present,
        "english_alignment_ok": english_alignment_ok,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": confirmed_text_hash,
        "corrected_text_hash": corrected_text_hash,
    }


def evaluate_coronation_title_es_helper_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    confirmed_text = row.get("confirmed_text") or ""
    corrected_text = row.get("corrected_text") or ""
    english_text = row.get("english_text") or ""
    source_key = row.get("source_key") or ""
    pattern = CORONATION_TITLE_ES_HELPER_PATTERNS.get(source_key) or {}
    corrected_text_hash = row.get("corrected_text_hash") or sha256_text(corrected_text)
    confirmed_text_hash = row.get("current_confirmed_text_hash") or sha256_text(confirmed_text)
    corrected_normalized = normalize_literal_key(corrected_text)
    english_normalized = normalize_literal_key(english_text)
    required_missing_tokens = tuple(pattern.get("required_missing_tokens") or ())
    required_corrected_tokens = tuple(pattern.get("required_corrected_tokens") or ())
    required_corrected_markers = tuple(pattern.get("required_corrected_markers") or ())
    english_markers = tuple(pattern.get("english_markers") or ())
    spanish_residue_markers = tuple(pattern.get("spanish_residue_markers") or ())

    boundary_policy_ok = row.get("boundary_policy") == "weak_auto_issue_inline_spanish_literal"
    required_missing_tokens_present = bool(
        required_missing_tokens and all(token in missing_tokens for token in required_missing_tokens)
    )
    confirmed_contains_missing_helpers = bool(
        required_missing_tokens and all(token in confirmed_text for token in required_missing_tokens)
    )
    corrected_removed_missing_helpers = bool(
        required_missing_tokens and all(token not in corrected_text for token in required_missing_tokens)
    )
    corrected_has_any_es_helper = bool(ES_HELPER_IN_TEXT_RE.search(corrected_text))
    corrected_tokens_preserved = bool(
        required_corrected_tokens and all(token in corrected_text for token in required_corrected_tokens)
    )
    corrected_markers_present = bool(
        required_corrected_markers
        and all(marker in corrected_normalized for marker in required_corrected_markers)
    )
    english_alignment_ok = bool(
        english_markers
        and all(normalize_literal_key(marker) in english_normalized for marker in english_markers)
    )
    spanish_residue_removed = bool(
        spanish_residue_markers
        and all(marker not in corrected_normalized for marker in spanish_residue_markers)
    )
    has_text_delta = corrected_text_hash != confirmed_text_hash
    allowed_source_key = bool(pattern and row.get("relative_path") == pattern.get("relative_path"))

    blockers: list[str] = []
    if not allowed_source_key:
        blockers.append("unsupported_coronation_title_key")
    if not boundary_policy_ok:
        blockers.append("wrong_boundary_policy")
    if row.get("policy_bucket") != CORONATION_TITLE_ES_HELPER_BUCKET:
        blockers.append("wrong_policy_bucket")
    if extra_tokens:
        blockers.append("unexpected_extra_tokens")
    if not required_missing_tokens_present:
        blockers.append("required_missing_es_helpers_not_present")
    if not confirmed_contains_missing_helpers:
        blockers.append("confirmed_text_missing_expected_es_helpers")
    if not corrected_removed_missing_helpers:
        blockers.append("corrected_text_still_has_expected_es_helpers")
    if corrected_has_any_es_helper:
        blockers.append("corrected_text_still_has_es_helper")
    if not corrected_tokens_preserved:
        blockers.append("corrected_missing_title_tokens")
    if not corrected_markers_present:
        blockers.append("corrected_markers_not_verified")
    if not spanish_residue_removed:
        blockers.append("spanish_residue_not_removed")
    if not english_alignment_ok:
        blockers.append("english_alignment_not_verified")
    if not has_text_delta:
        blockers.append("no_text_delta")
    if validation_issues:
        blockers.append("validation_issue")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_coronation_title_es_helper_neutralization_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "helper_mode": "coronation_title_es_helper_neutralization",
        "allowed_source_key": allowed_source_key,
        "matched_pattern_key": source_key if pattern else "",
        "required_missing_tokens_present": required_missing_tokens_present,
        "confirmed_contains_missing_helpers": confirmed_contains_missing_helpers,
        "corrected_removed_missing_helpers": corrected_removed_missing_helpers,
        "corrected_has_any_es_helper": corrected_has_any_es_helper,
        "corrected_tokens_preserved": corrected_tokens_preserved,
        "corrected_markers_present": corrected_markers_present,
        "spanish_residue_removed": spanish_residue_removed,
        "english_alignment_ok": english_alignment_ok,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": confirmed_text_hash,
        "corrected_text_hash": corrected_text_hash,
    }


def evaluate_nickname_whisperer_select_cstring_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    confirmed_text = row.get("confirmed_text") or ""
    corrected_text = row.get("corrected_text") or ""
    english_text = row.get("english_text") or ""
    source_key = row.get("source_key") or ""
    pattern = NICKNAME_WHISPERER_SELECT_CSTRING_PATTERNS.get(source_key) or {}
    refs = select_cstring_localplayer_literal_refs(confirmed_text)
    normalized_expected = tuple(pattern.get("normalized_literals") or ())
    matched_ref = next(
        (
            ref
            for ref in refs
            if ref["scope"] == pattern.get("scope")
            and (ref["normalized_left"], ref["normalized_right"]) == normalized_expected
        ),
        None,
    )
    corrected_text_hash = row.get("corrected_text_hash") or sha256_text(corrected_text)
    confirmed_text_hash = row.get("current_confirmed_text_hash") or sha256_text(confirmed_text)
    corrected_normalized = normalize_literal_key(corrected_text)
    english_normalized = normalize_literal_key(english_text)
    required_missing_tokens = tuple(pattern.get("required_missing_tokens") or ())
    required_confirmed_tokens = tuple(pattern.get("required_confirmed_tokens") or required_missing_tokens)
    required_corrected_tokens = tuple(pattern.get("required_corrected_tokens") or ())
    required_corrected_markers = tuple(pattern.get("required_corrected_markers") or ())
    english_markers = tuple(pattern.get("english_markers") or ())

    boundary_policy_ok = row.get("boundary_policy") == "weak_auto_issue_dynamic_spanish_literal"
    required_missing_tokens_present = bool(
        required_missing_tokens and all(token in missing_tokens for token in required_missing_tokens)
    )
    confirmed_contains_missing_tokens = bool(
        required_confirmed_tokens and all(token in confirmed_text for token in required_confirmed_tokens)
    )
    corrected_removed_missing_tokens = bool(
        required_missing_tokens and all(token not in corrected_text for token in required_missing_tokens)
    )
    corrected_has_select = "Select_CString(" in corrected_text
    corrected_has_any_es_helper = bool(ES_HELPER_IN_TEXT_RE.search(corrected_text))
    corrected_tokens_preserved = bool(
        required_corrected_tokens and all(token in corrected_text for token in required_corrected_tokens)
    )
    corrected_markers_present = bool(
        required_corrected_markers
        and all(marker in corrected_normalized for marker in required_corrected_markers)
    )
    english_alignment_ok = bool(
        english_markers
        and all(normalize_literal_key(marker) in english_normalized for marker in english_markers)
    )
    has_text_delta = corrected_text_hash != confirmed_text_hash
    allowed_source_key = bool(pattern and row.get("relative_path") == pattern.get("relative_path"))

    blockers: list[str] = []
    if not allowed_source_key:
        blockers.append("unsupported_nickname_whisperer_key")
    if not boundary_policy_ok:
        blockers.append("wrong_boundary_policy")
    if row.get("policy_bucket") != NICKNAME_WHISPERER_SELECT_CSTRING_BUCKET:
        blockers.append("wrong_policy_bucket")
    if extra_tokens:
        blockers.append("unexpected_extra_tokens")
    if not required_missing_tokens_present:
        blockers.append("required_missing_tokens_not_present")
    if not confirmed_contains_missing_tokens:
        blockers.append("confirmed_text_missing_expected_tokens")
    if not matched_ref:
        blockers.append("confirmed_redundant_select_cstring_not_verified")
    if not corrected_removed_missing_tokens:
        blockers.append("corrected_text_still_has_expected_tokens")
    if corrected_has_select:
        blockers.append("corrected_text_still_has_select_cstring")
    if corrected_has_any_es_helper:
        blockers.append("corrected_text_still_has_es_helper")
    if not corrected_tokens_preserved:
        blockers.append("corrected_missing_character_name_token")
    if not corrected_markers_present:
        blockers.append("corrected_markers_not_verified")
    if not english_alignment_ok:
        blockers.append("english_alignment_not_verified")
    if not has_text_delta:
        blockers.append("no_text_delta")
    if validation_issues:
        blockers.append("validation_issue")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_nickname_whisperer_select_cstring_neutralization_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "helper_mode": "nickname_whisperer_select_cstring_neutralization",
        "allowed_source_key": allowed_source_key,
        "matched_pattern_key": source_key if pattern else "",
        "required_missing_tokens_present": required_missing_tokens_present,
        "confirmed_contains_missing_tokens": confirmed_contains_missing_tokens,
        "confirmed_redundant_select_cstring": matched_ref is not None,
        "select_scope": matched_ref["scope"] if matched_ref else "",
        "redundant_literal_pair": list(normalized_expected),
        "corrected_removed_missing_tokens": corrected_removed_missing_tokens,
        "corrected_has_select_cstring": corrected_has_select,
        "corrected_has_any_es_helper": corrected_has_any_es_helper,
        "corrected_tokens_preserved": corrected_tokens_preserved,
        "corrected_markers_present": corrected_markers_present,
        "english_alignment_ok": english_alignment_ok,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": confirmed_text_hash,
        "corrected_text_hash": corrected_text_hash,
    }


def evaluate_single_combat_victor_name_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    confirmed_text = row.get("confirmed_text") or ""
    corrected_text = row.get("corrected_text") or ""
    english_text = row.get("english_text") or ""
    source_key = row.get("source_key") or ""
    pattern = SINGLE_COMBAT_VICTOR_NAME_PATTERNS.get(source_key) or {}
    corrected_text_hash = row.get("corrected_text_hash") or sha256_text(corrected_text)
    confirmed_text_hash = row.get("current_confirmed_text_hash") or sha256_text(confirmed_text)
    confirmed_normalized = normalize_literal_key(confirmed_text)
    corrected_normalized = normalize_literal_key(corrected_text)
    english_normalized = normalize_literal_key(english_text)
    required_extra_tokens = tuple(pattern.get("required_extra_tokens") or ())
    english_pronoun_tokens = tuple(pattern.get("english_pronoun_tokens") or ())
    required_corrected_markers = tuple(pattern.get("required_corrected_markers") or ())
    required_confirmed_markers = tuple(pattern.get("required_confirmed_markers") or ())
    required_quote_markers = tuple(pattern.get("required_quote_markers") or ())

    boundary_policy_ok = row.get("boundary_policy") == "weak_auto_residual_spanish_literal_repair"
    no_missing_tokens = not missing_tokens
    expected_extra_tokens = sorted(extra_tokens) == sorted(required_extra_tokens)
    corrected_name_count = corrected_text.count("[sc_victor.GetFirstNameNoTooltip]")
    confirmed_name_count = confirmed_text.count("[sc_victor.GetFirstNameNoTooltip]")
    corrected_has_required_names = corrected_name_count == len(required_extra_tokens)
    confirmed_did_not_have_name = confirmed_name_count == 0
    english_pronouns_present = bool(
        english_pronoun_tokens and all(token in english_text for token in english_pronoun_tokens)
    )
    corrected_markers_present = bool(
        required_corrected_markers
        and all(marker in corrected_normalized for marker in required_corrected_markers)
    )
    confirmed_markers_present = bool(
        required_confirmed_markers
        and all(marker in confirmed_normalized for marker in required_confirmed_markers)
    )
    quote_markers_preserved = bool(
        required_quote_markers
        and all(marker in corrected_normalized for marker in required_quote_markers)
        and all(marker in confirmed_normalized for marker in required_quote_markers)
    )
    english_alignment_ok = bool(
        english_pronouns_present
        and all(normalize_literal_key(marker) in english_normalized for marker in ("not wrong", "opportunity"))
    )
    has_text_delta = corrected_text_hash != confirmed_text_hash
    allowed_source_key = bool(pattern and row.get("relative_path") == pattern.get("relative_path"))

    blockers: list[str] = []
    if not allowed_source_key:
        blockers.append("unsupported_single_combat_key")
    if not boundary_policy_ok:
        blockers.append("wrong_boundary_policy")
    if row.get("policy_bucket") != SINGLE_COMBAT_VICTOR_NAME_BUCKET:
        blockers.append("wrong_policy_bucket")
    if not no_missing_tokens:
        blockers.append("unexpected_missing_tokens")
    if not expected_extra_tokens:
        blockers.append("unexpected_extra_tokens")
    if not corrected_has_required_names:
        blockers.append("corrected_missing_required_sc_victor_names")
    if not confirmed_did_not_have_name:
        blockers.append("confirmed_already_had_sc_victor_name")
    if not english_pronouns_present:
        blockers.append("english_pronouns_not_verified")
    if not corrected_markers_present:
        blockers.append("corrected_markers_not_verified")
    if not confirmed_markers_present:
        blockers.append("confirmed_markers_not_verified")
    if not quote_markers_preserved:
        blockers.append("quote_markers_not_preserved")
    if not english_alignment_ok:
        blockers.append("english_alignment_not_verified")
    if not has_text_delta:
        blockers.append("no_text_delta")
    if validation_issues:
        blockers.append("validation_issue")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_single_combat_victor_name_pronoun_neutralization_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "helper_mode": "single_combat_victor_name_pronoun_neutralization",
        "allowed_source_key": allowed_source_key,
        "matched_pattern_key": source_key if pattern else "",
        "no_missing_tokens": no_missing_tokens,
        "expected_extra_tokens": expected_extra_tokens,
        "corrected_name_count": corrected_name_count,
        "confirmed_name_count": confirmed_name_count,
        "corrected_has_required_names": corrected_has_required_names,
        "confirmed_did_not_have_name": confirmed_did_not_have_name,
        "english_pronouns_present": english_pronouns_present,
        "corrected_markers_present": corrected_markers_present,
        "confirmed_markers_present": confirmed_markers_present,
        "quote_markers_preserved": quote_markers_preserved,
        "english_alignment_ok": english_alignment_ok,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": confirmed_text_hash,
        "corrected_text_hash": corrected_text_hash,
    }


def normalized_select_pairs(text: str | None) -> list[tuple[str, str]]:
    return [
        (normalize_literal_key(left), normalize_literal_key(right))
        for left, right in select_cstring_literal_pairs(text)
    ]


def evaluate_tour_title_possessive_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    confirmed_text = row.get("confirmed_text") or ""
    corrected_text = row.get("corrected_text") or ""
    english_text = row.get("english_text") or ""
    source_key = row.get("source_key") or ""
    pattern = TOUR_TITLE_POSSESSIVE_PATTERNS.get(source_key) or {}
    corrected_text_hash = row.get("corrected_text_hash") or sha256_text(corrected_text)
    confirmed_text_hash = row.get("current_confirmed_text_hash") or sha256_text(confirmed_text)
    confirmed_normalized = normalize_literal_key(confirmed_text)
    corrected_normalized = normalize_literal_key(corrected_text)
    english_normalized = normalize_literal_key(english_text)
    required_extra_tokens = tuple(pattern.get("required_extra_tokens") or ())
    required_title_token = pattern.get("required_title_token") or ""
    possessive_pair = tuple(pattern.get("possessive_pair") or ())
    title_pair = tuple(pattern.get("title_pair") or ())
    confirmed_prefix = pattern.get("confirmed_prefix") or ""
    english_markers = tuple(pattern.get("english_markers") or ())
    confirmed_pairs = normalized_select_pairs(confirmed_text)
    corrected_pairs = normalized_select_pairs(corrected_text)

    boundary_policy_ok = row.get("boundary_policy") == "weak_auto_dynamic_select_cstring_spanish_literal"
    no_missing_tokens = not missing_tokens
    expected_extra_tokens = sorted(extra_tokens) == sorted(required_extra_tokens)
    title_token_preserved = bool(
        required_title_token
        and required_title_token in confirmed_text
        and required_title_token in corrected_text
    )
    confirmed_has_plain_possessive = bool(
        confirmed_prefix and confirmed_normalized.startswith(confirmed_prefix)
    )
    corrected_has_gendered_possessive = bool(possessive_pair and possessive_pair in corrected_pairs)
    confirmed_lacked_gendered_possessive = bool(possessive_pair and possessive_pair not in confirmed_pairs)
    title_pair_preserved = bool(title_pair and title_pair in confirmed_pairs and title_pair in corrected_pairs)
    corrected_starts_with_possessive = corrected_normalized.startswith(
        "[select_cstring( visiting_liege.isfemale, 'minha', 'meu' )]"
    )
    english_alignment_ok = bool(
        english_markers and all(marker in english_normalized for marker in english_markers)
    )
    has_text_delta = corrected_text_hash != confirmed_text_hash
    allowed_source_key = bool(pattern and row.get("relative_path") == pattern.get("relative_path"))

    blockers: list[str] = []
    if not allowed_source_key:
        blockers.append("unsupported_tour_title_key")
    if not boundary_policy_ok:
        blockers.append("wrong_boundary_policy")
    if row.get("policy_bucket") != TOUR_TITLE_POSSESSIVE_BUCKET:
        blockers.append("wrong_policy_bucket")
    if not no_missing_tokens:
        blockers.append("unexpected_missing_tokens")
    if not expected_extra_tokens:
        blockers.append("unexpected_extra_tokens")
    if not title_token_preserved:
        blockers.append("title_token_not_preserved")
    if not confirmed_has_plain_possessive:
        blockers.append("confirmed_plain_possessive_not_verified")
    if not corrected_has_gendered_possessive:
        blockers.append("corrected_gendered_possessive_not_verified")
    if not confirmed_lacked_gendered_possessive:
        blockers.append("confirmed_already_had_gendered_possessive")
    if not title_pair_preserved:
        blockers.append("hunter_title_pair_not_preserved")
    if not corrected_starts_with_possessive:
        blockers.append("corrected_possessive_not_at_title_start")
    if not english_alignment_ok:
        blockers.append("english_alignment_not_verified")
    if not has_text_delta:
        blockers.append("no_text_delta")
    if validation_issues:
        blockers.append("validation_issue")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_tour_title_gendered_possessive_neutralization_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "helper_mode": "tour_title_gendered_possessive_neutralization",
        "allowed_source_key": allowed_source_key,
        "matched_pattern_key": source_key if pattern else "",
        "no_missing_tokens": no_missing_tokens,
        "expected_extra_tokens": expected_extra_tokens,
        "title_token_preserved": title_token_preserved,
        "confirmed_has_plain_possessive": confirmed_has_plain_possessive,
        "corrected_has_gendered_possessive": corrected_has_gendered_possessive,
        "confirmed_lacked_gendered_possessive": confirmed_lacked_gendered_possessive,
        "hunter_title_pair_preserved": title_pair_preserved,
        "corrected_starts_with_possessive": corrected_starts_with_possessive,
        "english_alignment_ok": english_alignment_ok,
        "confirmed_select_pairs": [list(pair) for pair in confirmed_pairs],
        "corrected_select_pairs": [list(pair) for pair in corrected_pairs],
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": confirmed_text_hash,
        "corrected_text_hash": corrected_text_hash,
    }


def evaluate_ep3_travel_title_adjective_row(row: dict[str, Any], *, subpolicy_name: str) -> dict[str, Any]:
    missing_tokens = parse_json_list(row.get("missing_tokens_json"))
    extra_tokens = parse_json_list(row.get("extra_tokens_json"))
    validation_issues = blocking_validation_issues(row.get("corrected_text"))
    confirmed_text = row.get("confirmed_text") or ""
    corrected_text = row.get("corrected_text") or ""
    english_text = row.get("english_text") or ""
    source_key = row.get("source_key") or ""
    pattern = EP3_TRAVEL_TITLE_ADJECTIVE_PATTERNS.get(source_key) or {}
    corrected_text_hash = row.get("corrected_text_hash") or sha256_text(corrected_text)
    confirmed_text_hash = row.get("current_confirmed_text_hash") or sha256_text(confirmed_text)
    corrected_normalized = normalize_literal_key(corrected_text)
    english_normalized = normalize_literal_key(english_text)
    required_missing_tokens = tuple(pattern.get("required_missing_tokens") or ())
    required_extra_tokens = tuple(pattern.get("required_extra_tokens") or ())
    required_corrected_tokens = tuple(pattern.get("required_corrected_tokens") or ())
    required_confirmed_tokens = tuple(pattern.get("required_confirmed_tokens") or ())
    required_corrected_markers = tuple(pattern.get("required_corrected_markers") or ())
    english_markers = tuple(pattern.get("english_markers") or ())

    boundary_policy_ok = row.get("boundary_policy") == "weak_auto_residual_spanish_literal_repair"
    required_missing_tokens_present = bool(
        required_missing_tokens and sorted(missing_tokens) == sorted(required_missing_tokens)
    )
    expected_extra_tokens = bool(
        required_extra_tokens and sorted(extra_tokens) == sorted(required_extra_tokens)
    )
    confirmed_tokens_present = bool(
        required_confirmed_tokens and all(token in confirmed_text for token in required_confirmed_tokens)
    )
    corrected_tokens_present = bool(
        required_corrected_tokens and all(token in corrected_text for token in required_corrected_tokens)
    )
    corrected_removed_es_helper = "Custom('ES_" not in corrected_text
    corrected_removed_old_title_name = "[lords_liege.GetPrimaryTitle.GetNameNoTooltip]" not in corrected_text
    corrected_removed_title_style = "[lords_liege.GetTitleAsName|l]" not in corrected_text
    corrected_markers_present = bool(
        required_corrected_markers
        and all(marker in corrected_normalized for marker in required_corrected_markers)
    )
    english_alignment_ok = bool(
        english_markers
        and all(normalize_literal_key(marker) in english_normalized for marker in english_markers)
    )
    has_text_delta = corrected_text_hash != confirmed_text_hash
    allowed_source_key = bool(pattern and row.get("relative_path") == pattern.get("relative_path"))

    blockers: list[str] = []
    if not allowed_source_key:
        blockers.append("unsupported_ep3_travel_key")
    if not boundary_policy_ok:
        blockers.append("wrong_boundary_policy")
    if row.get("policy_bucket") != EP3_TRAVEL_TITLE_ADJECTIVE_BUCKET:
        blockers.append("wrong_policy_bucket")
    if not required_missing_tokens_present:
        blockers.append("required_missing_tokens_not_matched")
    if not expected_extra_tokens:
        blockers.append("unexpected_extra_tokens")
    if not confirmed_tokens_present:
        blockers.append("confirmed_tokens_not_verified")
    if not corrected_tokens_present:
        blockers.append("corrected_tokens_not_verified")
    if not corrected_removed_es_helper:
        blockers.append("corrected_still_has_es_helper")
    if not corrected_removed_old_title_name:
        blockers.append("corrected_still_has_old_title_name")
    if not corrected_removed_title_style:
        blockers.append("corrected_still_has_title_style_variant")
    if not corrected_markers_present:
        blockers.append("corrected_markers_not_verified")
    if not english_alignment_ok:
        blockers.append("english_alignment_not_verified")
    if not has_text_delta:
        blockers.append("no_text_delta")
    if validation_issues:
        blockers.append("validation_issue")

    if blockers:
        status = "shadow_blocked"
        action = "hold_for_manual_token_policy_review"
        block_reason = ",".join(blockers)
    else:
        status = "shadow_ready"
        action = "would_accept_ep3_travel_title_adjective_alignment_shadow"
        block_reason = ""

    evidence = {
        "subpolicy": subpolicy_name,
        "helper_mode": "ep3_travel_title_adjective_alignment",
        "allowed_source_key": allowed_source_key,
        "matched_pattern_key": source_key if pattern else "",
        "required_missing_tokens_present": required_missing_tokens_present,
        "expected_extra_tokens": expected_extra_tokens,
        "confirmed_tokens_present": confirmed_tokens_present,
        "corrected_tokens_present": corrected_tokens_present,
        "corrected_removed_es_helper": corrected_removed_es_helper,
        "corrected_removed_old_title_name": corrected_removed_old_title_name,
        "corrected_removed_title_style": corrected_removed_title_style,
        "corrected_markers_present": corrected_markers_present,
        "english_alignment_ok": english_alignment_ok,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issue_codes": [issue.get("code") for issue in validation_issues],
    }
    return {
        **row,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "subpolicy_status": status,
        "subpolicy_action": action,
        "block_reason": block_reason,
        "evidence": evidence,
        "current_confirmed_text_hash": confirmed_text_hash,
        "corrected_text_hash": corrected_text_hash,
    }


def insert_run(
    conn,
    *,
    bridge_run_id: int,
    subpolicy_name: str,
    rows: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    started_at: datetime,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    statuses = Counter(row["subpolicy_status"] for row in rows)
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_runs (
            rule_version,
            bridge_run_id,
            subpolicy_name,
            subpolicy_status,
            total_candidates,
            shadow_ready_count,
            blocked_count,
            validation_issue_count,
            report_path,
            csv_path,
            jsonl_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            bridge_run_id,
            subpolicy_name,
            "shadow",
            len(rows),
            statuses["shadow_ready"],
            len(rows) - statuses["shadow_ready"],
            sum(1 for row in rows if row["validation_issue_count"] > 0),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def insert_items(conn, *, run_id: int, bridge_run_id: int, rows: list[dict[str, Any]]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_items (
                run_id,
                bridge_run_id,
                bridge_item_id,
                repair_queue_item_id,
                boundary_policy_item_id,
                review_decision_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                boundary_agent_key,
                boundary_policy,
                policy_bucket,
                risk_level,
                subpolicy_status,
                subpolicy_action,
                block_reason,
                evidence_json,
                validation_issue_count,
                current_confirmed_text_hash,
                corrected_text_hash,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                bridge_run_id,
                row["bridge_item_id"],
                row["repair_queue_item_id"],
                row["boundary_policy_item_id"],
                row["review_decision_id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row["boundary_agent_key"],
                row["boundary_policy"],
                row["policy_bucket"],
                row["risk_level"],
                row["subpolicy_status"],
                row["subpolicy_action"],
                row["block_reason"],
                json.dumps(row["evidence"], ensure_ascii=False, sort_keys=True),
                row["validation_issue_count"],
                row.get("current_confirmed_text_hash"),
                row.get("corrected_text_hash"),
                now,
            ),
        )
        row["subpolicy_item_id"] = int(cursor.lastrowid)


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    bridge_run_id: int,
    subpolicy_name: str,
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> None:
    fieldnames = [
        "subpolicy_item_id",
        "bridge_item_id",
        "repair_queue_item_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "boundary_policy",
        "policy_bucket",
        "risk_level",
        "subpolicy_status",
        "subpolicy_action",
        "block_reason",
        "evidence",
        "confirmed_text",
        "corrected_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{field: row.get(field) for field in fieldnames},
                    "evidence": json.dumps(row["evidence"], ensure_ascii=False, sort_keys=True),
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames if field != "evidence"},
                "evidence": row["evidence"],
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
                "old_text": row.get("old_text"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    statuses = Counter(row["subpolicy_status"] for row in rows)
    policies = Counter(row["boundary_policy"] for row in rows)
    verbs = Counter()
    glossary_ids = Counter()
    helper_scopes = Counter()
    helper_modes = Counter()
    for row in rows:
        for verb in row["evidence"].get("matched_ptbr_verbs", []):
            verbs[verb] += 1
        for pair in row["evidence"].get("glossary_visible_label_pairs", []):
            glossary_ids[pair.get("new_glossary_id") or pair.get("old_glossary_id")] += 1
        if row["evidence"].get("helper_scope"):
            helper_scopes[row["evidence"]["helper_scope"]] += 1
        if row["evidence"].get("helper_mode"):
            helper_modes[row["evidence"]["helper_mode"]] += 1

    lines = [
        "Auto-confirmation boundary token subpolicy shadow",
        f"Rule version: {RULE_VERSION}",
        f"Subpolicy: {subpolicy_name}",
        f"Run id: {run_id}",
        f"Bridge run id: {bridge_run_id}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Shadow ready: {statuses['shadow_ready']:,}",
        f"- Blocked: {len(rows) - statuses['shadow_ready']:,}",
        f"- By status: {json.dumps(dict(statuses), ensure_ascii=False, sort_keys=True)}",
        f"- By boundary policy: {json.dumps(dict(policies), ensure_ascii=False, sort_keys=True)}",
        f"- Matched PT-BR verbs: {json.dumps(dict(verbs), ensure_ascii=False, sort_keys=True)}",
        f"- Glossary IDs: {json.dumps(dict(glossary_ids), ensure_ascii=False, sort_keys=True)}",
        f"- Helper scopes: {json.dumps(dict(helper_scopes), ensure_ascii=False, sort_keys=True)}",
        f"- Helper modes: {json.dumps(dict(helper_modes), ensure_ascii=False, sort_keys=True)}",
        "",
        "Shadow-ready sample:",
    ]
    for row in [item for item in rows if item["subpolicy_status"] == "shadow_ready"][:30]:
        lines.extend(
            [
                f"- item {row['subpolicy_item_id']} | bridge {row['bridge_item_id']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']}",
                f"  confirmed={short(row.get('confirmed_text'))}",
                f"  corrected={short(row.get('corrected_text'))}",
            ]
        )
    blocked_rows = [item for item in rows if item["subpolicy_status"] != "shadow_ready"]
    lines.extend(["", "Blocked sample:"])
    if blocked_rows:
        for row in blocked_rows[:30]:
            lines.extend(
                [
                    f"- {row['block_reason']} | bridge {row['bridge_item_id']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']}",
                    f"  corrected={short(row.get('corrected_text'))}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow subpolicy only: no output writes, no confirmation updates, no token-policy decision approval.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, bridge_run_id: int | None = None, subpolicy_name: str = SELECT_CSTRING_SUBPOLICY) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_bridge_run_id = bridge_run_id or latest_bridge_run_id(conn)
        rows = fetch_rows(conn, bridge_run_id=selected_bridge_run_id, subpolicy_name=subpolicy_name)
        txt_path, csv_path, jsonl_path = report_paths(settings, subpolicy_name=subpolicy_name)
        run_id = insert_run(
            conn,
            bridge_run_id=selected_bridge_run_id,
            subpolicy_name=subpolicy_name,
            rows=rows,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            started_at=started_at,
        )
        insert_items(conn, run_id=run_id, bridge_run_id=selected_bridge_run_id, rows=rows)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            bridge_run_id=selected_bridge_run_id,
            subpolicy_name=subpolicy_name,
            rows=rows,
            started_at=started_at,
        )
        conn.commit()

    counts = Counter(row["subpolicy_status"] for row in rows)
    print("[auto_confirmation_reopen_text_boundary_token_subpolicy_shadow] Shadow subpolicy generated")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_shadow] Run id: {run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_shadow] Bridge run id: {selected_bridge_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_shadow] Subpolicy: {subpolicy_name}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_shadow] Candidates: {len(rows):,}")
    for key, value in counts.most_common():
        print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_shadow] {key}: {value:,}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_shadow] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_shadow] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_shadow] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "bridge_run_id": selected_bridge_run_id,
        "subpolicy_name": subpolicy_name,
        "total_candidates": len(rows),
        "counts": dict(counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run shadow subpolicies over boundary token-policy bridge rows.")
    parser.add_argument("--bridge-run-id", type=int, default=None)
    parser.add_argument("--subpolicy-name", default=SELECT_CSTRING_SUBPOLICY)
    args = parser.parse_args()
    main(bridge_run_id=args.bridge_run_id, subpolicy_name=args.subpolicy_name)
