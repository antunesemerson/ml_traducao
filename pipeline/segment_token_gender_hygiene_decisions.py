from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import short, structural_tokens
from ml_composite_subpolicy_guarded_overlay import text_hygiene_flags


RULE_VERSION = "segment_token_gender_hygiene_decisions_v1"
QUEUE_GLOB = "*_segment_token_gender_subpolicy_status_fix_text_before_learning.jsonl"


CURATED_REPLACEMENTS: dict[int, list[tuple[str, str]]] = {
    39949: [
        ("liderar? a entrada", "liderar\u00e1 a entrada"),
        ("caber ? figura", "caber \u00e0 figura"),
    ],
    39951: [
        ("d? um passo", "d\u00e1 um passo"),
        ("inclina a cabe\u00e7a ? indecifr\u00e1vel", "inclina a cabe\u00e7a, indecifr\u00e1vel"),
    ],
    39967: [
        ("que \u00c0s vezes voc\u00ea ? melhor", "que \u00e0s vezes voc\u00ea \u00e9 melhor"),
    ],
    39996: [("compreend?-la", "compreend\u00ea-la")],
    39997: [("n\u00e3o ? dif\u00edcil", "n\u00e3o \u00e9 dif\u00edcil")],
    40009: [
        ("] ? capaz", "] \u00e9 capaz"),
        ("] ? pelo menos", "] \u2014 pelo menos"),
    ],
    40021: [("[marriage_rival.GetFirstName] ? eu cuspo", "[marriage_rival.GetFirstName] \u2014 eu cuspo")],
    40027: [
        ("pela f\u00e9ria", "pela f\u00faria"),
        ("[concerned_friend.GetFirstName] ? interrompid", "[concerned_friend.GetFirstName] \u00e9 interrompid"),
    ],
    40038: [
        ("]... ? como", "]... \u00e9 como"),
        ("chego ? porta", "chego \u00e0 porta"),
        ("encontrar l? [ROOT.Char.Custom('CatStoryName')]", "encontrar l\u00e1 [ROOT.Char.Custom('CatStoryName')]"),
        ("Avan\u00e3o pela sala", "Avan\u00e7o pela sala"),
    ],
    40039: [
        ("lan\u00e3ou", "lan\u00e7ou"),
        ("conspirat\u00e9rio", "conspirat\u00f3rio"),
        ("quest\u00e1es matrimoniais", "quest\u00f5es matrimoniais"),
        ("habilidade f\u00e9sica", "habilidade f\u00edsica"),
        ("venc?-l[khutulun.Custom('ES_OA')]", "venc\u00ea-l[khutulun.Custom('ES_OA')]"),
    ],
    40041: [("me v? colocar", "me v\u00ea colocar")],
    40052: [("]! ? voc\u00ea?", "]! \u00c9 voc\u00ea?")],
    40062: [
        ("se v? reduzid", "se v\u00ea reduzid"),
        ("reconsider?-lo", "reconsider\u00e1-lo"),
    ],
    40067: [
        ("; \u00c0s vezes", "; \u00e0s vezes"),
        (")] ? acusad", ")] \u00e9 acusad"),
    ],
    40070: [("proteg?-l[ROOT.Char.Custom('ES_OA')]", "proteg\u00ea-l[ROOT.Char.Custom('ES_OA')]")],
    40200: [
        ("se juntar ? nossa causa", "se juntar \u00e0 nossa causa"),
        ("influ\u00eancia ? incompar\u00e1vel", "influ\u00eancia \u00e9 incompar\u00e1vel"),
    ],
    40239: [("se sentir? tra\u00edd", "se sentir\u00e1 tra\u00edd")],
    40263: [
        ("dor ? seguido", "dor \u00e9 seguido"),
        ("p\u00fanico", "p\u00e2nico"),
    ],
    40279: [
        ("classifica\u00e7\u00e3o ? procurei", "classifica\u00e7\u00e3o \u2014 procurei"),
        ("p\u00fanico", "p\u00e2nico"),
        ("traz?-las", "traz\u00ea-las"),
    ],
    40301: [
        ("parabeniz?-lo", "parabeniz\u00e1-lo"),
        ("at\u00e9nitas", "at\u00f4nitas"),
        ("at\u00e9nitos", "at\u00f4nitos"),
    ],
    40323: [("D? a [guru_or_chaplain.GetHerHim]", "D\u00ea a [guru_or_chaplain.GetHerHim]")],
    40357: [
        ("Como ? triste", "Como \u00e9 triste"),
        ("v?-los", "v\u00ea-los"),
    ],
    40366: [("junto ? amurada", "junto \u00e0 amurada")],
    40461: [("oferecer? nenhum", "oferecer\u00e1 nenhum")],
    40499: [
        ("hoje ? especialmente", "hoje \u00e9 especialmente"),
        ("\n\n? ira o que", "\n\n\u00c9 ira o que"),
    ],
    40500: [("acabar? adormecendo", "acabar\u00e1 adormecendo")],
    40523: [
        ("trouxe ? tona", "trouxe \u00e0 tona"),
        ("faz?-l[disobedient_subject.Custom('ES_OA')]", "faz\u00ea-l[disobedient_subject.Custom('ES_OA')]"),
        ("mencion?-lo", "mencion\u00e1-lo"),
    ],
    40559: [("[scoped_prisoner.GetSheHe|U] ? [scoped_prisoner.GetWomanMan]", "[scoped_prisoner.GetSheHe|U] \u00e9 [scoped_prisoner.GetWomanMan]")],
    40595: [("resultado ? que", "resultado \u00e9 que")],
    40604: [("abra\u00e7o ? r\u00e1pido", "abra\u00e7o \u00e9 r\u00e1pido")],
}


def latest_queue_path(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    candidates = sorted(reports_dir.glob(QUEUE_GLOB), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No queue found with glob {QUEUE_GLOB}")
    return candidates[0]


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def apply_replacements(text: str, replacements: list[tuple[str, str]]) -> tuple[str, list[str], list[str]]:
    updated = text
    hits: list[str] = []
    missing: list[str] = []
    for before, after in replacements:
        if before not in updated:
            missing.append(before)
            continue
        updated = updated.replace(before, after)
        hits.append(before)
    return updated, hits, missing


def validator_issue_codes(text: str) -> set[str]:
    issues = local_quality_validator.validate_text(text)["issues"]
    return {str(issue.get("code")) for issue in issues}


def classify(row: dict[str, Any]) -> dict[str, Any]:
    policy_item_id = int(row["policy_item_id"])
    current = row.get("confirmed_text") or ""
    replacements = CURATED_REPLACEMENTS.get(policy_item_id)
    if not replacements:
        return {
            **row,
            "decision": "defer_manual_review",
            "corrected_text": "",
            "hygiene_status": "no_curated_replacements",
            "hygiene_reasons": ["no_curated_replacements"],
        }

    corrected, hits, missing = apply_replacements(current, replacements)
    reasons = [f"hit:{hit}" for hit in hits]
    if missing:
        return {
            **row,
            "decision": "defer_manual_review",
            "corrected_text": "",
            "hygiene_status": "blocked_missing_expected_fragment",
            "hygiene_reasons": [*reasons, *[f"missing:{item}" for item in missing]],
        }
    if corrected == current:
        return {
            **row,
            "decision": "defer_manual_review",
            "corrected_text": "",
            "hygiene_status": "blocked_no_text_change",
            "hygiene_reasons": [*reasons, "corrected_text_same_as_current"],
        }
    if structural_tokens(current) != structural_tokens(corrected):
        return {
            **row,
            "decision": "defer_manual_review",
            "corrected_text": "",
            "hygiene_status": "blocked_token_change",
            "hygiene_reasons": [*reasons, "corrected_text_changes_structural_tokens"],
        }

    flags = text_hygiene_flags(corrected)
    issue_codes = validator_issue_codes(corrected)
    blocking_issue_codes = {
        "replacement_question_mark_mojibake",
        "utf8_mojibake_sequence",
    }
    if flags or issue_codes.intersection(blocking_issue_codes):
        return {
            **row,
            "decision": "defer_manual_review",
            "corrected_text": "",
            "hygiene_status": "blocked_still_suspicious",
            "hygiene_reasons": [
                *reasons,
                *[f"text_hygiene:{flag}" for flag in flags],
                *[f"validator:{code}" for code in sorted(issue_codes.intersection(blocking_issue_codes))],
            ],
        }

    return {
        **row,
        "decision": "fix_confirmed_text",
        "corrected_text": corrected,
        "hygiene_status": "ready_fix_confirmed_text",
        "hygiene_reasons": [f"rule:{RULE_VERSION}", *reasons],
    }


def write_outputs(
    settings: dict[str, Any],
    *,
    source_queue: Path,
    rows: list[dict[str, Any]],
) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    decisions_path = reports_dir / f"{timestamp}_segment_token_gender_hygiene_decisions.jsonl"
    report_path = reports_dir / f"{timestamp}_segment_token_gender_hygiene_decisions.txt"

    with decisions_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            if row["decision"] != "fix_confirmed_text":
                continue
            handle.write(
                json.dumps(
                    {
                        "policy_item_id": row["policy_item_id"],
                        "policy_run_id": row["policy_run_id"],
                        "decision": "fix_confirmed_text",
                        "corrected_text": row["corrected_text"],
                        "reviewer": "codex_gender_hygiene_curator",
                        "notes": "; ".join(row["hygiene_reasons"]),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    counts = Counter(row["hygiene_status"] for row in rows)
    lines = [
        "Segment token gender hygiene decisions",
        f"Rule version: {RULE_VERSION}",
        f"Source queue: {source_queue}",
        f"Rows inspected: {len(rows)}",
        f"Decision rows emitted: {sum(1 for row in rows if row['decision'] == 'fix_confirmed_text')}",
        f"Decisions JSONL: {decisions_path}",
        "",
        "Status counts:",
        *[f"- {key}: {value}" for key, value in counts.most_common()],
        "",
        "Preview:",
    ]
    for row in rows:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['hygiene_status']} | {row['gender_subtype']} | {row['source_key']}"
                ),
                f"  reasons: {json.dumps(row['hygiene_reasons'], ensure_ascii=False)}",
                f"  before: {short(row.get('confirmed_text'), 260)}",
                f"  after:  {short(row.get('corrected_text'), 260)}",
            ]
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path, decisions_path


def main(*, queue_path: str | None = None) -> None:
    settings = db.load_settings()
    source_queue = db.project_path(queue_path) if queue_path else latest_queue_path(settings)
    raw_rows = load_rows(source_queue)
    rows = [classify(row) for row in raw_rows]
    report_path, decisions_path = write_outputs(settings, source_queue=source_queue, rows=rows)
    counts = Counter(row["hygiene_status"] for row in rows)

    print("[segment_token_gender_hygiene_decisions] Decisions generated")
    print(f"[segment_token_gender_hygiene_decisions] Rule version: {RULE_VERSION}")
    print(f"[segment_token_gender_hygiene_decisions] Source queue: {source_queue}")
    print(f"[segment_token_gender_hygiene_decisions] Rows inspected: {len(rows)}")
    for key, value in counts.most_common():
        print(f"[segment_token_gender_hygiene_decisions] {key}: {value}")
    print(f"[segment_token_gender_hygiene_decisions] Report: {report_path}")
    print(f"[segment_token_gender_hygiene_decisions] Decisions JSONL: {decisions_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate curated fix decisions for gender-token hygiene rows.")
    parser.add_argument("--queue", default=None)
    args = parser.parse_args()
    main(queue_path=args.queue)
