from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_dynamic_ck3_pattern_shadow_v1"
POLICY_NAME = "dynamic_ck3_pattern_shadow"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_dynamic_ck3_expression"
ISSUE_FAMILY = "dynamic_ck3_expression_microagent"
ALLOWED_QUEUE_STRATEGIES = {"partial_coverage_composition", "stratified_issue_ledger"}
SELECT_CSTRING_SIMPLE_RE = re.compile(
    r"Select_CString\(\s*([^,]+?)\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)",
    re.IGNORECASE,
)


def stable_hash(value: str | None) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def evidence_matches_current(*, evidence_text: str, current_text: str) -> bool:
    if evidence_text == current_text:
        return True
    if evidence_text.endswith("..."):
        preview = evidence_text[:-3]
        return bool(preview) and current_text.startswith(preview)
    return False


def is_select_cstring_same_payload_noop_pattern(*, bucket: str, text: str) -> bool:
    if bucket not in {"dynamic_select_cstring_long", "dynamic_select_cstring_short"}:
        return False
    raw_count = text.count("Select_CString(")
    if raw_count <= 0:
        return False
    matches = list(SELECT_CSTRING_SIMPLE_RE.finditer(text))
    if len(matches) != raw_count:
        return False
    for match in matches:
        left = match.group(2).strip()
        right = match.group(3).strip()
        if not left or left != right:
            return False
    return True


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_dynamic_ck3_pattern_shadow"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_queue_runs
        WHERE agent_key = ?
          AND issue_family = ?
          AND queue_strategy IN ('partial_coverage_composition', 'stratified_issue_ledger')
          AND finished_at IS NOT NULL
          AND selected_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (AGENT_KEY, ISSUE_FAMILY),
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed dynamic CK3 queue found for the allowed strategies.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_dynamic_ck3_pattern_shadow_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            agent_key TEXT NOT NULL,
            queue_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            subpolicy_counts_json TEXT,
            action_counts_json TEXT,
            blocker_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_dynamic_ck3_pattern_shadow_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            queue_bucket TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            shadow_status TEXT NOT NULL,
            shadow_action TEXT NOT NULL,
            shadow_allowed INTEGER NOT NULL,
            block_reason TEXT,
            current_confirmed_text_hash TEXT,
            queue_confirmed_text_hash TEXT,
            evidence_text_hash TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_dynamic_ck3_pattern_shadow_runs(id) ON DELETE CASCADE
        )
        """
    )


def fetch_queue_run(conn, *, queue_run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_issue_review_queue_runs WHERE id = ?", (queue_run_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"Issue review queue run not found: {queue_run_id}")
    return dict(row)


def fetch_rows(conn, *, queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            q.*,
            l.issue_family AS ledger_issue_family,
            l.issue_kind AS ledger_issue_kind,
            c.confirmed_text AS current_confirmed_text,
            c.locked AS confirmation_locked
        FROM ml_issue_review_queue_items q
        JOIN ml_issue_ledger_items l ON l.id = q.ledger_item_id
        LEFT JOIN segment_confirmations c
          ON c.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = q.segment_id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        WHERE q.run_id = ?
        ORDER BY q.relative_path, q.source_line_number, q.source_key, q.id
        """,
        (queue_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def has_disallowed_dynamic_surface(text: str) -> bool:
    markers = (
        ".Custom('ES_",
        ".Custom(\"ES_",
        "LocalPlayerString",
        "años",
        "año",
        "debería",
        "deberías",
        "quedó",
        "quedaste",
        "vosotros",
        "vosotras",
        "Desaprueba",
    )
    return any(marker in text for marker in markers)


def is_hostage_memory_relation_pattern(
    *,
    bucket: str,
    relative_path: str,
    source_key: str,
    text: str,
) -> bool:
    if bucket != "dynamic_custom_localization":
        return False
    if relative_path != "memories_l_spanish.yml":
        return False
    if not source_key.startswith("hostage_"):
        return False
    if len(text) > 360:
        return False
    required_markers = (
        "[hostage.Custom('ES_OA')]",
        "[hostage.Custom('ES_ElLa')]",
        "[hostage.GetTitledFirstName|l]",
        "Custom2('RelationToMeShort'",
    )
    if not all(marker in text for marker in required_markers):
        return False
    return any(
        marker in text
        for marker in (
            "[home_court.Custom('ES_",
            "[home_court.GetFullName]",
            "[owner.Custom('ES_",
        )
    )


def is_hostage_memory_death_reason_pattern(
    *,
    bucket: str,
    relative_path: str,
    source_key: str,
    text: str,
) -> bool:
    if bucket != "dynamic_custom_localization":
        return False
    if relative_path != "memories_l_spanish.yml":
        return False
    if not source_key.startswith("hostage_died_desc_"):
        return False
    if len(text) > 180:
        return False
    return (
        "[hostage.GetFirstName]" in text
        and "[hostage.Custom('ES_OA')]" in text
        and "[owner.GetName]" in text
        and ("[hostage.GetDeathReason]" in text or "[hostage.GetDeathReasonHideKiller]" in text)
    )


def is_interaction_haggler_aptitude_value_pattern(
    *,
    bucket: str,
    relative_path: str,
    source_key: str,
    text: str,
    english: str,
) -> bool:
    if bucket != "dynamic_interactions_activities":
        return False
    if relative_path != "interactions_l_spanish.yml":
        return False
    if source_key != "has_person_haggler_decrease_ransom_cost":
        return False
    if len(text) > 180:
        return False
    required_text = (
        "[aptitude|E]",
        "[GetCourtPositionType('person_haggler_camp_officer').GetName()|l]",
        "[haggler.GetFirstName]",
        "#P [new_ransom_value|1]#!",
        "[gold_i]",
    )
    required_english = (
        "[haggler.GetFirstNamePossessive]",
        "[GetCourtPositionType('person_haggler_camp_officer').GetName()]",
        "[aptitude|E]",
        "[new_ransom_value|1]",
    )
    return all(marker in text for marker in required_text) and all(
        marker in english for marker in required_english
    )


def is_single_combat_enthusiastic_onslaught_pattern(
    *,
    bucket: str,
    relative_path: str,
    source_key: str,
    text: str,
    english: str,
) -> bool:
    if bucket != "dynamic_custom_localization":
        return False
    if relative_path != "single_combat_events_l_spanish.yml":
        return False
    if source_key != "single_combat.0041.desc.opponent_response.enthusiastic_onslaught":
        return False
    if len(text) > 360:
        return False
    required_text = (
        "[sc_loser.GetFirstNameNoTooltip]",
        "[sc_loser.Custom('SignatureWeaponFlourishPresentParticiple')]",
        "[sc_loser.Custom('signature_weapon')]",
        "[sc_loser.Custom('ES_DelDela')]",
        "amador[sc_loser.Custom('ES_OA')]",
        "desesperado[sc_loser.Custom('ES_OA')]",
    )
    required_english = (
        "[sc_loser.GetFirstNameNoTooltip]",
        "[sc_loser.Custom('SignatureWeaponFlourishPresentParticiple')]",
        "[sc_loser.Custom('signature_weapon')]",
        "desperate amateur",
    )
    return all(marker in text for marker in required_text) and all(
        marker in english for marker in required_english
    )


def is_tournament_bout_relation_decided_pattern(
    *,
    bucket: str,
    relative_path: str,
    source_key: str,
    text: str,
    english: str,
) -> bool:
    if bucket != "dynamic_custom_localization":
        return False
    if relative_path != "dlc/ep2/tournament/dlc_ep2_contest_events_l_spanish.yml":
        return False
    if source_key not in {"contest_events.0810.both_relation", "contest_events.0810.winner_relation"}:
        return False
    if len(text) > 340:
        return False
    required_text = (
        "[ROOT.Char.Custom2('RelationToMeShort', contest_winner)]",
        "[contest_winner.Custom('ES_ElLa')]",
        "[contest_winner.GetTitledFirstName|l]",
        "[contest_loser.Custom('ES_ElLa')]",
        "[contest_loser.GetTitledFirstName|l]",
        "foi decidida",
    )
    required_english = (
        "The bout between",
        "[contest_winner.GetTitledFirstName]",
        "[contest_loser.GetTitledFirstName]",
        "has been decided",
    )
    return all(marker in text for marker in required_text) and all(
        marker in english for marker in required_english
    )


def is_japan_administrative_defeat_independence_pattern(
    *,
    bucket: str,
    relative_path: str,
    source_key: str,
    text: str,
    english: str,
) -> bool:
    if bucket != "dynamic_select_cstring_long":
        return False
    if relative_path != "dlc/tgp/dlc_tgp_japan_wars_l_spanish.yml":
        return False
    if source_key != "japan_demand_administrative_cb_defeat_desc_independence":
        return False
    if len(text) > 320:
        return False
    required_text = (
        "[attacker.GetShortUIName|U]",
        "[attacker.Custom('ES_ElLa')]",
        "[attacker.GetTopLiege.GetTitleAsNameNoTooltip|l]",
        "[defender.GetShortUIName|U]",
        "[Select_CString(defender.IsLocalPlayer, 'se tornar\u00e3o', 'se tornar\u00e3o')]",
        "[realms|lE]",
        "[soryo|lE]",
        "[independent|lE]s",
    )
    required_english = (
        "[attacker.GetShortUIName|U]",
        "[defender.GetShortUIName]",
        "will become [independent|E]",
        "[soryo|E]",
        "[realms|E]",
    )
    return all(marker in text for marker in required_text) and all(
        marker in english for marker in required_english
    )


def is_tournament_memory_won_contest_pattern(
    *,
    bucket: str,
    relative_path: str,
    source_key: str,
    text: str,
    english: str,
) -> bool:
    if bucket != "dynamic_custom_localization":
        return False
    if relative_path != "memories_l_spanish.yml":
        return False
    if source_key != "tournament_won_contest_memory_desc_third_perspective":
        return False
    if len(text) > 280:
        return False
    required_text = (
        "[contestant.Custom('ES_ElLa')|U]",
        "[contestant.GetTitledFirstName]",
        "[ROOT.Var('contest_type').GetFlagName]",
        "[GetActivityType( 'activity_tournament' ).GetName|l]",
        "[host.Custom('ES_DelDela')]",
        "[host.GetTitledFirstName]",
    )
    required_english = (
        "[contestant.GetTitledFirstName] won",
        "[ROOT.Var('contest_type').GetFlagName] contest",
        "[host.GetTitledFirstNamePossessive]",
        "[GetActivityType( 'activity_tournament' ).GetName]",
    )
    return all(marker in text for marker in required_text) and all(
        marker in english for marker in required_english
    )


def is_battlefield_would_be_usurper_intro_pattern(
    *,
    bucket: str,
    relative_path: str,
    source_key: str,
    text: str,
    english: str,
) -> bool:
    if bucket != "dynamic_custom_localization":
        return False
    if relative_path != "game_rules_l_spanish.yml":
        return False
    if source_key != "game_rule.1141.intro.battlefield":
        return False
    if len(text) > 290:
        return False
    required_text = (
        "[winner.Custom('ES_ElLa')|U]",
        "[winner.GetTitledFirstName|l]",
        "[loser.Custom('ES_ElLa')]",
        "usurpador[loser.Custom('ES_XA')]",
        "[loser.GetTitledFirstNameNicknamed|l]",
    )
    required_english = (
        "[winner.GetTitledFirstName] has perished on the battlefield",
        "would-be usurper",
        "[loser.GetTitledFirstNameNicknamed]",
    )
    return all(marker in text for marker in required_text) and all(
        marker in english for marker in required_english
    )


def is_evict_adventurer_camp_realm_timer_pattern(
    *,
    bucket: str,
    relative_path: str,
    source_key: str,
    text: str,
    english: str,
) -> bool:
    if bucket != "dynamic_select_cstring_long":
        return False
    if relative_path != "dlc/ep3/ep3_interactions_l_spanish.yml":
        return False
    if source_key != "evict_adventurer_consequences_tt":
        return False
    if len(text) > 280:
        return False
    required_text = (
        "[camp|lE]",
        "[Select_CString( CHARACTER.IsLocalPlayer, 'seu personagem', CHARACTER.GetShortUIName)]",
        "[realm|E]",
        "[Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'voc\u00ea', TARGET_CHARACTER.GetShortUIName)]",
        "#V 3#!",
    )
    required_english = (
        "[CHARACTER.GetShortUINamePossessiveNoTooltip]",
        "[camp|E]",
        "[TARGET_CHARACTER.GetShortUINamePossessive]",
        "[realm|E]",
        "after #V 3#! months",
    )
    return all(marker in text for marker in required_text) and all(
        marker in english for marker in required_english
    )


def classify_pattern(row: dict[str, Any]) -> tuple[str, str]:
    bucket = row.get("queue_bucket") or ""
    relative_path = row.get("relative_path") or ""
    source_key = row.get("source_key") or ""
    text = row.get("current_confirmed_text") or ""
    english = row.get("english_text") or ""

    if is_hostage_memory_relation_pattern(
        bucket=bucket,
        relative_path=relative_path,
        source_key=source_key,
        text=text,
    ):
        return "dynamic_hostage_memory_relation_custom_loc", "would_cover_dynamic_hostage_memory_relation_shadow"

    if is_hostage_memory_death_reason_pattern(
        bucket=bucket,
        relative_path=relative_path,
        source_key=source_key,
        text=text,
    ):
        return "dynamic_hostage_memory_death_reason_custom_loc", "would_cover_dynamic_hostage_memory_death_reason_shadow"

    if is_interaction_haggler_aptitude_value_pattern(
        bucket=bucket,
        relative_path=relative_path,
        source_key=source_key,
        text=text,
        english=english,
    ):
        return "dynamic_interaction_haggler_aptitude_value_line", "would_cover_dynamic_interaction_haggler_aptitude_value_shadow"

    if is_single_combat_enthusiastic_onslaught_pattern(
        bucket=bucket,
        relative_path=relative_path,
        source_key=source_key,
        text=text,
        english=english,
    ):
        return "dynamic_single_combat_enthusiastic_onslaught", "would_cover_dynamic_single_combat_enthusiastic_onslaught_shadow"

    if is_tournament_bout_relation_decided_pattern(
        bucket=bucket,
        relative_path=relative_path,
        source_key=source_key,
        text=text,
        english=english,
    ):
        return "dynamic_tournament_bout_relation_decided", "would_cover_dynamic_tournament_bout_relation_decided_shadow"

    if is_japan_administrative_defeat_independence_pattern(
        bucket=bucket,
        relative_path=relative_path,
        source_key=source_key,
        text=text,
        english=english,
    ):
        return "dynamic_japan_administrative_defeat_independence", "would_cover_dynamic_japan_administrative_defeat_independence_shadow"

    if is_tournament_memory_won_contest_pattern(
        bucket=bucket,
        relative_path=relative_path,
        source_key=source_key,
        text=text,
        english=english,
    ):
        return "dynamic_tournament_memory_won_contest", "would_cover_dynamic_tournament_memory_won_contest_shadow"

    if is_battlefield_would_be_usurper_intro_pattern(
        bucket=bucket,
        relative_path=relative_path,
        source_key=source_key,
        text=text,
        english=english,
    ):
        return "dynamic_battlefield_would_be_usurper_intro", "would_cover_dynamic_battlefield_would_be_usurper_intro_shadow"

    if is_evict_adventurer_camp_realm_timer_pattern(
        bucket=bucket,
        relative_path=relative_path,
        source_key=source_key,
        text=text,
        english=english,
    ):
        return "dynamic_evict_adventurer_camp_realm_timer", "would_cover_dynamic_evict_adventurer_camp_realm_timer_shadow"

    if len(text) > 260 or has_disallowed_dynamic_surface(text):
        return "dynamic_ck3_unclassified", "hold_for_manual_dynamic_ck3_review"

    if bucket == "dynamic_rules_tooltips" and relative_path in {
        "triggers/character_triggers_l_spanish.yml",
        "effects_l_spanish.yml",
    }:
        if any(token in text for token in ("[trait_level_track_xp|lE]", "[piety_level|lE]", "[prestige_level|lE]")):
            if any(token in text for token in ("$NUM|V0$", "$VALUE|V0$", "[TRAIT.GetName(", "[GetPietyLevelName(", "[GetPrestigeLevelName(")):
                if english:
                    return "dynamic_rule_tooltip_label", "would_cover_dynamic_rule_tooltip_shadow"

    if bucket == "dynamic_general_context" and "[GetScheme(" in text:
        if "[success_chance|E]" in text or "[scheme_success_chance|E]" in text:
            if relative_path in {
                "culture/traditions/cultural_traditions_l_spanish.yml",
                "religion/religion_core_tenets_l_spanish.yml",
            } and english:
                return "dynamic_scheme_success_label", "would_cover_dynamic_scheme_success_shadow"

    if (
        bucket == "dynamic_select_cstring_short"
        and relative_path == "effects_l_spanish.yml"
        and source_key == "GET_RIVAL"
        and text == "Torna-se [rival|lE] de [Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'você', TARGET_CHARACTER.GetShortUIName )]"
        and english == "Becomes [TARGET_CHARACTER.GetShortUINamePossessive] [rival|E]"
    ):
        return "dynamic_select_cstring_relation_label", "would_cover_dynamic_select_cstring_relation_shadow"

    if is_select_cstring_same_payload_noop_pattern(bucket=bucket, text=text):
        return "dynamic_select_cstring_same_payload_noop", "would_cover_dynamic_select_cstring_same_payload_noop_shadow"

    return "dynamic_ck3_unclassified", "hold_for_manual_dynamic_ck3_review"


def evaluate_row(row: dict[str, Any], *, global_reasons: list[str]) -> dict[str, Any]:
    subpolicy_name, shadow_action = classify_pattern(row)
    blockers = list(global_reasons)
    queue_text = row.get("confirmed_text") or ""
    current_text = row.get("current_confirmed_text") or ""
    evidence_text = row.get("evidence_text") or ""

    if row.get("ledger_issue_family") != ISSUE_FAMILY:
        blockers.append("ledger_family_mismatch")
    if row.get("issue_family") != ISSUE_FAMILY:
        blockers.append("queue_family_mismatch")
    if row.get("agent_key") != AGENT_KEY:
        blockers.append("queue_agent_mismatch")
    if int(row.get("confirmation_locked") or 0):
        blockers.append("locked_confirmation")
    if not current_text:
        blockers.append("missing_current_confirmation")
    if queue_text and current_text != queue_text:
        blockers.append("stale_confirmation_text_changed")
    if evidence_text and not evidence_matches_current(evidence_text=evidence_text, current_text=current_text):
        blockers.append("evidence_text_mismatch")
    if subpolicy_name == "dynamic_ck3_unclassified":
        blockers.append("unclassified_dynamic_ck3_pattern")

    shadow_allowed = 0 if blockers else 1
    shadow_status = "shadow_ready_pattern" if shadow_allowed else "shadow_blocked"
    return {
        **row,
        "subpolicy_name": subpolicy_name,
        "shadow_status": shadow_status,
        "shadow_action": shadow_action if shadow_allowed else "hold_for_manual_dynamic_ck3_review",
        "shadow_allowed": shadow_allowed,
        "block_reason": ",".join(blockers) if blockers else "",
        "current_confirmed_text_hash": stable_hash(current_text),
        "queue_confirmed_text_hash": stable_hash(queue_text),
        "evidence_text_hash": stable_hash(evidence_text),
    }


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    queue_run: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    fields = [
        "shadow_item_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "queue_bucket",
        "subpolicy_name",
        "shadow_status",
        "shadow_action",
        "shadow_allowed",
        "block_reason",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {field: row.get(field) for field in fields}
            payload["confirmed_preview"] = short(row.get("current_confirmed_text"))
            payload["english_preview"] = short(row.get("english_text"))
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    status_counts = Counter(row["shadow_status"] for row in rows)
    subpolicy_counts = Counter(row["subpolicy_name"] for row in rows if row["shadow_allowed"])
    blocker_counts = Counter(row["block_reason"] or "none" for row in rows)
    lines = [
        "Issue dynamic CK3 pattern shadow",
        f"Rule version: {RULE_VERSION}",
        f"Policy: {POLICY_NAME} ({POLICY_STATUS})",
        f"Run id: {run_id}",
        f"Queue run id: {queue_run['id']}",
        f"Queue strategy: {queue_run.get('queue_strategy')}",
        f"Ledger run id: {queue_run['ledger_run_id']}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Ready: {sum(1 for row in rows if row['shadow_allowed']):,}",
        f"- Blocked: {sum(1 for row in rows if not row['shadow_allowed']):,}",
        "",
        "Ready subpolicies:",
        *[f"- {key}: {value:,}" for key, value in subpolicy_counts.most_common()],
        "",
        "Blockers:",
        *[f"- {key}: {value:,}" for key, value in blocker_counts.most_common()],
        "",
        "Statuses:",
        *[f"- {key}: {value:,}" for key, value in status_counts.most_common()],
        "",
        "Ready samples:",
    ]
    for row in [item for item in rows if item["shadow_allowed"]][:30]:
        lines.append(f"- {row['subpolicy_name']} | {row['relative_path']}::{row['source_key']}")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow only: no source/output writes, no confirmations, no production promotion.",
            "- This covers only recognized CK3 dynamic-expression patterns with narrow path/key/token guards.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    txt_path, csv_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        queue_run = fetch_queue_run(conn, queue_run_id=selected_queue_run_id)
        source_rows = fetch_rows(conn, queue_run_id=selected_queue_run_id)
        global_reasons: list[str] = []
        if queue_run.get("agent_key") != AGENT_KEY:
            global_reasons.append("queue_run_agent_mismatch")
        if queue_run.get("issue_family") != ISSUE_FAMILY:
            global_reasons.append("queue_run_family_mismatch")
        if queue_run.get("queue_strategy") not in ALLOWED_QUEUE_STRATEGIES:
            global_reasons.append("queue_run_strategy_not_allowed")
        if not source_rows:
            global_reasons.append("no_pattern_candidate_rows")
        rows = [evaluate_row(row, global_reasons=global_reasons) for row in source_rows]

        ready_count = sum(1 for row in rows if row["shadow_allowed"])
        blocked_count = len(rows) - ready_count
        subpolicy_counts = Counter(row["subpolicy_name"] for row in rows if row["shadow_allowed"])
        action_counts = Counter(row["shadow_action"] for row in rows if row["shadow_allowed"])
        blocker_counts = Counter(row["block_reason"] or "none" for row in rows)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_dynamic_ck3_pattern_shadow_runs (
                rule_version,
                policy_name,
                policy_status,
                agent_key,
                queue_run_id,
                ledger_run_id,
                candidate_count,
                ready_count,
                blocked_count,
                subpolicy_counts_json,
                action_counts_json,
                blocker_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                AGENT_KEY,
                selected_queue_run_id,
                queue_run["ledger_run_id"],
                len(rows),
                ready_count,
                blocked_count,
                json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(action_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(blocker_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO ml_issue_dynamic_ck3_pattern_shadow_items (
                    run_id,
                    queue_run_id,
                    queue_item_id,
                    ledger_run_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    queue_bucket,
                    issue_family,
                    issue_kind,
                    subpolicy_name,
                    shadow_status,
                    shadow_action,
                    shadow_allowed,
                    block_reason,
                    current_confirmed_text_hash,
                    queue_confirmed_text_hash,
                    evidence_text_hash,
                    notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_queue_run_id,
                    row["id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["queue_bucket"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["subpolicy_name"],
                    row["shadow_status"],
                    row["shadow_action"],
                    int(row["shadow_allowed"]),
                    row["block_reason"],
                    row["current_confirmed_text_hash"],
                    row["queue_confirmed_text_hash"],
                    row["evidence_text_hash"],
                    "",
                    now,
                ),
            )
            row["shadow_item_id"] = int(item_cursor.lastrowid)
        conn.commit()

    write_outputs(txt_path=txt_path, csv_path=csv_path, jsonl_path=jsonl_path, run_id=run_id, queue_run=queue_run, rows=rows)
    print("[issue_dynamic_ck3_pattern_shadow] Shadow generated")
    print(f"[issue_dynamic_ck3_pattern_shadow] Run id: {run_id}")
    print(f"[issue_dynamic_ck3_pattern_shadow] Queue run id: {selected_queue_run_id}")
    print(f"[issue_dynamic_ck3_pattern_shadow] Candidates: {len(rows):,}")
    print(f"[issue_dynamic_ck3_pattern_shadow] Ready: {ready_count:,}")
    print(f"[issue_dynamic_ck3_pattern_shadow] Blocked: {blocked_count:,}")
    print(f"[issue_dynamic_ck3_pattern_shadow] Report: {txt_path}")
    return {
        "run_id": run_id,
        "queue_run_id": selected_queue_run_id,
        "candidate_count": len(rows),
        "ready_count": ready_count,
        "blocked_count": blocked_count,
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create shadow coverage for recognized CK3 dynamic-expression patterns.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
