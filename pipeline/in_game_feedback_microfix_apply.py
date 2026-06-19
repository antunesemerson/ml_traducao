from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import replace_quoted_text


RULE_VERSION = "in_game_feedback_microfix_apply_v2_1"
CONFIRMATION_SOURCE = "in_game_feedback_microfix_production"
CONFIRMATION_LABEL = "20260613_in_game_feedback_microfix_v2"
AUTO_LEVEL = "auto_confirmed"
HUMAN_LEVEL = "human_confirmed"
REVIEWER = "learning_front_in_game_feedback_microfix"

TARGETS: dict[int, dict[str, Any]] = {
    183577: {
        "relative_path": "names/character_names_l_spanish.yml",
        "source_key": "Job",
        "current_text": "Trabalho",
        "proposed_text": "Jó",
        "allow_locked": False,
        "reason": "biblical name Job should be Jó in PT-BR, not literal Trabalho",
    },
    107702: {
        "relative_path": "event_localization/birth_events_l_spanish.yml",
        "source_key": "birth.1001.same_gender_heir.desc",
        "current_text": "Um dia, pequen[child.Custom('ES_InIna')], você dará continuidade ao meu legado. Como chamar um[child.Custom('ES_XA')] [ROOT.Char.GetTitleAsNameNoTooltip]?",
        "proposed_text": "Um dia, pequen[child.Custom('ES_OA')], você dará continuidade ao meu legado. Como chamar um[child.Custom('ES_XA')] [ROOT.Char.GetTitleAsNameNoTooltip]?",
        "allow_locked": False,
        "reason": "avoid Spanish diminutive token ES_InIna rendering as pequenín",
    },
    107713: {
        "relative_path": "event_localization/birth_events_l_spanish.yml",
        "source_key": "birth.1001.a",
        "current_text": "Que você cresça forte e sábi[child.Custom('ES_OA')], meu/minha [child.Custom('GetDaughterSon')].",
        "proposed_text": "Que você cresça forte e sábi[child.Custom('ES_OA')], [Select_CString( child.IsFemale, 'minha', 'meu' )] [child.Custom('GetDaughterSon')].",
        "allow_locked": True,
        "reason": "replace visible meu/minha with CK3 gender-aware Select_CString",
    },
    107735: {
        "relative_path": "event_localization/birth_events_l_spanish.yml",
        "source_key": "birth.1003.a",
        "current_text": "Que você cresça forte e sábi[child.Custom('ES_OA')], meu/minha [child.Custom('GetDaughterSon')].",
        "proposed_text": "Que você cresça forte e sábi[child.Custom('ES_OA')], [Select_CString( child.IsFemale, 'minha', 'meu' )] [child.Custom('GetDaughterSon')].",
        "allow_locked": True,
        "reason": "replace visible meu/minha with CK3 gender-aware Select_CString",
    },
    243970: {
        "relative_path": "schemes_l_spanish.yml",
        "source_key": "sway_sway_failed_message",
        "current_text": "[target.GetShortUINameNoTooltip] não foi persuadido[target.Custom('ES_OA')]",
        "proposed_text": "[target.GetShortUINameNoTooltip] não se convenceu",
        "allow_locked": False,
        "reason": "natural gender-neutral sway failure toast",
    },
    123171: {
        "relative_path": "event_localization/lifestyle/warfare/learn_commander_trait_events_l_spanish.yml",
        "source_key": "learn_commander_trait.12.b",
        "current_text": "Não, eu não entendi completamente o que [teacher.GetFirstNameNoTooltip] tem tentado me ensinar.",
        "proposed_text": "Ainda não aprendi tudo com [teacher.GetFirstNameNoTooltip].",
        "allow_locked": False,
        "reason": "compact option text for event button",
    },
    137763: {
        "relative_path": "event_localization/yearly_events/bp1_yearly_events_chad_l_spanish.yml",
        "source_key": "bp1_yearly.3100.desc",
        "current_text": "Meu/Minha [ROOT.Char.Custom2('RelationToMe', SCOPE.sC('3100_courtier_liege'))] [3100_courtier_liege.GetTitledFirstName] me honrou com uma visita e trouxe um de seus [3100_courtier_liege.Custom('GetCourtierPlural')] consigo. [3100_courtier_liege.GetFirstNameNoTooltip] se aproxima de mim, animad[3100_courtier_liege.Custom('ES_OA')]:\\n\\n\\\"Prazer em ver você, [ROOT.Char.GetTitledFirstNameNoTooltip]! Alegra-me ver que prospera aqui em [ROOT.Char.GetCapitalLocation.GetName]. Ocorreu-me que [3100_target_courtier.GetFirstName], aqui presente, poderia ser de utilidade em sua [ROOT.Char.Custom('GetCourt')]. ",
        "current_file_text": "Meu/Minha [ROOT.Char.Custom2('RelationToMe', SCOPE.sC('3100_courtier_liege'))] [3100_courtier_liege.GetTitledFirstName] me honrou com uma visita e trouxe um de seus [3100_courtier_liege.Custom('GetCourtierPlural')] consigo. [3100_courtier_liege.GetFirstNameNoTooltip] se aproxima de mim, animad[3100_courtier_liege.Custom('ES_OA')]:\\n\\n\"Prazer em ver você, [ROOT.Char.GetTitledFirstNameNoTooltip]! Alegra-me ver que prospera aqui em [ROOT.Char.GetCapitalLocation.GetName]. Ocorreu-me que [3100_target_courtier.GetFirstName], aqui presente, poderia ser de utilidade em sua [ROOT.Char.Custom('GetCourt')]. ",
        "proposed_text": "[ROOT.Char.Custom2('RelationToMe', SCOPE.sC('3100_courtier_liege'))|U] [3100_courtier_liege.GetTitledFirstName] me honrou com uma visita e trouxe um de seus [3100_courtier_liege.Custom('GetCourtierPlural')] consigo. [3100_courtier_liege.GetFirstNameNoTooltip] se aproxima de mim, animad[3100_courtier_liege.Custom('ES_OA')]:\\n\\n\"Prazer em ver você, [ROOT.Char.GetTitledFirstNameNoTooltip]! Alegra-me ver que prospera aqui em [ROOT.Char.GetCapitalLocation.GetName]. Ocorreu-me que [3100_target_courtier.GetFirstName], aqui presente, poderia ser de utilidade em sua [ROOT.Char.Custom('GetCourt')]. ",
        "allow_locked": False,
        "reason": "avoid visible Meu/Minha before dynamic family relation by capitalizing the relation directly",
    },
    23366: {
        "relative_path": "custom_localization/greeting_custom_loc_l_spanish.yml",
        "source_key": "greeting_family_liked",
        "current_text": "Meu querid[crush.Custom('ES_OA')] [ROOT.Char.Custom2('RelationToMeShort', second)],",
        "proposed_text": "[ROOT.Char.Custom2('RelationToMeShort', second)|U],",
        "allow_locked": False,
        "reason": "remove undefined crush gender token and render a clean family letter salutation",
    },
    23367: {
        "relative_path": "custom_localization/greeting_custom_loc_l_spanish.yml",
        "source_key": "greeting_family_fallback",
        "current_text": "Saudações [ROOT.Char.Custom2('RelationToMeShort', second)],",
        "proposed_text": "[ROOT.Char.Custom2('RelationToMeShort', second)|U],",
        "allow_locked": False,
        "reason": "make family letter fallback compact and capitalized, e.g. Sobrinho,",
    },
    130945: {
        "relative_path": "event_localization/secret_events/secret_bastard_events_l_spanish.yml",
        "source_key": "secret_bastard.001.opening",
        "current_text": "Meu [ROOT.Char.Custom2('RelationToMeShort', SCOPE.sC('mother'))], [mother.Custom('ES_ElLa')] [mother.GetTitledFirstName|l], há algum tempo vem mostrando sinais de gravidez,",
        "proposed_text": "Minha [ROOT.Char.Custom2('RelationToMeShort', SCOPE.sC('mother'))], [mother.Custom('ES_ElLa')] [mother.GetTitledFirstName|l], há algum tempo vem mostrando sinais de gravidez,",
        "allow_locked": False,
        "reason": "mother scope is female; fix visible Meu conhecida to Minha conhecida",
    },
    156329: {
        "relative_path": "interactions_l_spanish.yml",
        "source_key": "court_interaction",
        "current_text": "Romancear #weak (Esquema)#!",
        "proposed_text": "Cortejar #weak (Esquema)#!",
        "allow_locked": False,
        "reason": "in-game UI label: Romance scheme action is better localized as Cortejar, not Romancear",
    },
    156330: {
        "relative_path": "interactions_l_spanish.yml",
        "source_key": "court_interaction_notification",
        "current_text": "Romancear",
        "proposed_text": "Cortejar",
        "allow_locked": False,
        "reason": "in-game UI label: Romance scheme notification should use Cortejar",
    },
    156743: {
        "relative_path": "interactions_l_spanish.yml",
        "source_key": "I_HAVE_ROMANCE_COOLDOWN_ON_THIS_CHARACTER",
        "current_text": "Você não pode romancear [TARGET_CHARACTER.GetShortUIName] novamente por algum tempo",
        "proposed_text": "Você não pode voltar a cortejar [TARGET_CHARACTER.GetShortUIName] por algum tempo",
        "allow_locked": False,
        "reason": "in-game UI text: avoid inelegant Romancear and align with Spanish cortejar/courting action",
    },
}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def latest_confirmation(conn, segment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM segment_confirmations
        WHERE segment_id = ?
        ORDER BY updated_at DESC, confirmed_at DESC, id DESC
        LIMIT 1
        """,
        (segment_id,),
    ).fetchone()
    return dict(row) if row else None


def fetch_live_rows(conn) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in TARGETS)
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.is_active,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.output_line_number,
            o.portuguese_text AS output_text
        FROM source_segments s
        JOIN output_segments o
          ON o.segment_id = s.id
        WHERE s.id IN ({placeholders})
        """,
        tuple(TARGETS),
    ).fetchall()
    live = {int(row["segment_id"]): dict(row) for row in rows}
    if set(live) != set(TARGETS):
        raise RuntimeError(f"Missing target rows: {sorted(set(TARGETS) - set(live))}")
    return live


def read_file_text(output_root: Path, row: dict[str, Any]) -> tuple[Path, list[str], int, str, str]:
    output_path = output_root / Path(as_text(row["relative_path"]))
    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    line_index = int(row["output_line_number"]) - 1
    if line_index < 0 or line_index >= len(lines):
        raise RuntimeError(f"Output line out of range for segment {row['segment_id']}.")
    raw_line = lines[line_index]
    first_quote = raw_line.find('"')
    last_quote = raw_line.rfind('"')
    if first_quote < 0 or last_quote <= first_quote:
        raise RuntimeError(f"Line has no quoted localization value for segment {row['segment_id']}.")
    file_text = raw_line[first_quote + 1 : last_quote].replace('\\"', '"')
    return output_path, lines, line_index, raw_line, file_text


def evaluate(conn, output_root: Path) -> list[dict[str, Any]]:
    live_rows = fetch_live_rows(conn)
    results: list[dict[str, Any]] = []
    for segment_id, target in TARGETS.items():
        row = live_rows[segment_id]
        status = "ready"
        reasons: list[str] = []
        if int(row["is_active"] or 0) != 1:
            reasons.append("source_not_active")
        if row["relative_path"] != target["relative_path"]:
            reasons.append("relative_path_mismatch")
        if row["source_key"] != target["source_key"]:
            reasons.append("source_key_mismatch")

        confirmation = latest_confirmation(conn, segment_id)
        locked = bool(confirmation and int(confirmation.get("locked") or 0))
        if locked and not target["allow_locked"]:
            reasons.append("locked_confirmation_not_allowed")

        if as_text(row["output_text"]) == target["proposed_text"]:
            status = "already_applied"
        elif as_text(row["output_text"]) != target["current_text"]:
            reasons.append("db_output_text_unexpected")

        try:
            output_path, lines, line_index, raw_line, file_text = read_file_text(output_root, row)
        except RuntimeError as exc:
            reasons.append(str(exc))
            output_path = None
            lines = []
            line_index = -1
            raw_line = ""
            file_text = ""
        else:
            expected_file_text = target.get("current_file_text", target["current_text"])
            if file_text == target["proposed_text"] and status == "already_applied":
                pass
            elif file_text != expected_file_text:
                reasons.append("file_output_text_unexpected")

        if reasons:
            status = "blocked"
        results.append(
            {
                "segment_id": segment_id,
                "relative_path": target["relative_path"],
                "source_key": target["source_key"],
                "current_text": target["current_text"],
                "proposed_text": target["proposed_text"],
                "reason": target["reason"],
                "allow_locked": target["allow_locked"],
                "locked": locked,
                "status": status,
                "reasons": reasons,
                "_row": row,
                "_output_path": output_path,
                "_lines": lines,
                "_line_index": line_index,
                "_raw_line": raw_line,
            }
        )
    return results


def upsert_confirmation(conn, *, item: dict[str, Any], now: str) -> bool:
    segment_id = int(item["segment_id"])
    confirmation = latest_confirmation(conn, segment_id)
    locked = bool(confirmation and int(confirmation.get("locked") or 0))
    confirmation_level = HUMAN_LEVEL if locked else AUTO_LEVEL
    already = (
        confirmation
        and as_text(confirmation.get("confirmation_level")) == confirmation_level
        and as_text(confirmation.get("confirmation_source")) == CONFIRMATION_SOURCE
        and as_text(confirmation.get("confirmation_label")) == CONFIRMATION_LABEL
        and as_text(confirmation.get("confirmed_text")) == item["proposed_text"]
        and int(confirmation.get("locked") or 0) == int(locked)
    )
    conn.execute(
        """
        INSERT INTO segment_confirmations (
            segment_id,
            confirmation_level,
            confirmed_text,
            confirmation_source,
            confirmation_label,
            locked,
            confidence_score,
            reviewer,
            confirmed_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?)
        ON CONFLICT(segment_id) DO UPDATE SET
            confirmation_level = excluded.confirmation_level,
            confirmed_text = excluded.confirmed_text,
            confirmation_source = excluded.confirmation_source,
            confirmation_label = excluded.confirmation_label,
            locked = excluded.locked,
            confidence_score = excluded.confidence_score,
            reviewer = excluded.reviewer,
            confirmed_at = COALESCE(segment_confirmations.confirmed_at, excluded.confirmed_at),
            updated_at = excluded.updated_at
        """,
        (
            segment_id,
            confirmation_level,
            item["proposed_text"],
            CONFIRMATION_SOURCE,
            CONFIRMATION_LABEL,
            1 if locked else 0,
            REVIEWER,
            now,
            now,
        ),
    )
    return not already


def write_report(settings: dict[str, Any], *, mode: str, results: list[dict[str, Any]], summary: dict[str, int]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{now_stamp()}_in_game_feedback_microfix_{mode}.txt"
    lines = [
        "In-game feedback microfix",
        f"Rule version: {RULE_VERSION}",
        f"Mode: {mode}",
        "",
        "Summary:",
    ]
    for key in (
        "candidates",
        "ready",
        "already_applied",
        "blocked",
        "applied",
        "confirmation_promoted",
        "output_written",
        "files_touched",
        "locked_feedback_updates",
    ):
        lines.append(f"- {key}: {summary.get(key, 0)}")
    lines.extend(["", "Items:"])
    for item in results:
        lines.extend(
            [
                f"- {item['segment_id']} | {item['relative_path']}::{item['source_key']} | {item['status']}",
                f"  before: {item['current_text']}",
                f"  after:  {item['proposed_text']}",
                f"  reason: {item['reason']}",
                f"  locked: {item['locked']}",
                f"  blocks: {', '.join(item['reasons']) if item['reasons'] else 'none'}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(*, apply: bool) -> Path:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    conn = db.connect(settings)
    applied = 0
    confirmation_promoted = 0
    output_written = 0
    files_touched = 0
    locked_feedback_updates = 0
    try:
        results = evaluate(conn, output_root)
        blocked = [item for item in results if item["status"] == "blocked"]
        ready = [item for item in results if item["status"] == "ready"]
        already = [item for item in results if item["status"] == "already_applied"]
        if blocked:
            conn.rollback()
        elif apply:
            by_file: dict[Path, list[dict[str, Any]]] = defaultdict(list)
            for item in ready:
                by_file[item["_output_path"]].append(item)
            now = datetime.now().isoformat(timespec="seconds")
            for output_path, items in sorted(by_file.items(), key=lambda pair: str(pair[0])):
                lines = items[0]["_lines"]
                for item in sorted(items, key=lambda row: int(row["_line_index"])):
                    line_index = int(item["_line_index"])
                    lines[line_index] = replace_quoted_text(item["_raw_line"], item["proposed_text"])
                    conn.execute(
                        """
                        UPDATE output_segments
                        SET portuguese_text = ?,
                            output_raw_line = ?,
                            portuguese_hash = ?,
                            last_indexed_at = ?
                        WHERE segment_id = ?
                        """,
                        (
                            item["proposed_text"],
                            lines[line_index],
                            sha256_text(item["proposed_text"]),
                            now,
                            int(item["segment_id"]),
                        ),
                    )
                    output_written += 1
                output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
                files_touched += 1
            for item in ready + already:
                if upsert_confirmation(conn, item=item, now=now):
                    confirmation_promoted += 1
                    if item["locked"]:
                        locked_feedback_updates += 1
            conn.commit()
            applied = len(ready)
            results = evaluate(conn, output_root)
        else:
            conn.rollback()

        summary = {
            "candidates": len(results),
            "ready": sum(1 for item in results if item["status"] == "ready"),
            "already_applied": sum(1 for item in results if item["status"] == "already_applied"),
            "blocked": sum(1 for item in results if item["status"] == "blocked"),
            "applied": applied,
            "confirmation_promoted": confirmation_promoted,
            "output_written": output_written,
            "files_touched": files_touched,
            "locked_feedback_updates": locked_feedback_updates,
        }
        mode = "apply" if apply else "dry_run"
        return write_report(settings, mode=mode, results=results, summary=summary)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = run(apply=args.apply)
    print(f"Report: {report}")


if __name__ == "__main__":
    main()
