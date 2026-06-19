from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_single_issue_residual_assisted_decisions_v1"
REVIEWER = "codex_assisted_short_label_residual_v1"


SPANISH_OR_FOREIGN_FRAGMENTS = (
    "gano ",
    "seran",
    "serán",
    " de inmediato",
    " a #emp mí",
    "cómo ",
    " osáis",
    "probabilidad",
    "reducción",
    "interludio iranio",
    "sonido",
    "the beneficiary",
)


def short(text: str | None, limit: int = 180) -> str:
    value = (text or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    return value if len(value) <= limit else value[: limit - 3] + "..."


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_single_issue_residual_assisted_decisions_queue_{queue_run_id}"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def fetch_queue_items(conn, queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.id,
            item.run_id,
            item.ledger_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.queue_bucket,
            item.evidence_text,
            item.confirmed_text,
            item.english_text,
            item.spanish_text
        FROM ml_issue_review_queue_items item
        WHERE item.run_id = ?
        ORDER BY item.queue_bucket, item.id
        """,
        (queue_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def has_foreign_fragment(text: str) -> bool:
    lower = text.lower()
    return any(fragment in lower for fragment in SPANISH_OR_FOREIGN_FRAGMENTS)


def normalize_bold_no(text: str) -> str:
    repaired = text
    replacements = (
        ("#bold No#!", "#bold Não#!"),
        ("#bold no#!", "#bold não#!"),
        ("#bold not#!", "#bold não#!"),
    )
    for old, new in replacements:
        repaired = repaired.replace(old, new)

    repaired = repaired.replace("#bold Não#! não ", "#bold Não#! ")
    repaired = repaired.replace("#bold não#! não ", "#bold não#! ")
    return repaired


def normalize_bol_marker(text: str) -> str:
    repaired = text
    repaired = re.sub(r"#BOL\b", "#bold", repaired)
    repaired = re.sub(r"#bol\b", "#bold", repaired)
    return repaired


def repair_markup_or_no(text: str) -> tuple[str, str | None]:
    repaired = normalize_bold_no(normalize_bol_marker(normalize_bold_no(text)))
    if repaired == text:
        return "false_positive_reopen", None
    if has_foreign_fragment(repaired):
        return "needs_domain_context", None
    return "needs_repair", repaired


def repair_spanish_lexical(row: dict[str, Any], text: str) -> tuple[str, str | None]:
    key = str(row.get("source_key") or "")
    if key == "end_the_struggle_oath_decision_title_persian_ended":
        return (
            "needs_repair",
            text.replace("Interludio iranio", "Intermezzo Iraniano"),
        )
    if key == "protected_poi_modifier_desc":
        return (
            "needs_repair",
            text.replace(
                "Probabilidade de ponto de [skill|lE] por [monument_expedition|lE] de:",
                "Chance de ponto de [skill|lE] por [monument_expedition|lE]:",
            ),
        )
    if key == "debate_argument_moderate_decrease_desc":
        return (
            "needs_repair",
            "#N Redução moderada#! da sua #V chance de sucesso#!",
        )
    if key == "debate_argument_moderate_increase_desc":
        return (
            "needs_repair",
            "#P Aumento moderado#! da sua #V chance de sucesso#!",
        )
    if key == "HEADER_AUDIO":
        return ("needs_repair", "#credits_header ÁUDIO#!")
    return "needs_new_microagent", None


def repair_english_visible(row: dict[str, Any], text: str) -> tuple[str, str | None]:
    key = str(row.get("source_key") or "")
    if key == "fp3_turkic_invasion_cb_victory_desc":
        return "needs_repair", "O beneficiário recebe os [titles|lE] contestados."
    if key == "fp3_turkic_invasion_beneficiary":
        return "needs_repair", "O beneficiário"
    return "needs_new_microagent", None


def classify_dialogue(text: str) -> str:
    if has_foreign_fragment(text):
        return "needs_new_microagent"
    return "needs_domain_context"


def decide(row: dict[str, Any]) -> dict[str, Any]:
    bucket = str(row.get("queue_bucket") or "")
    text = str(row.get("confirmed_text") or row.get("evidence_text") or "")
    decision = "needs_domain_context"
    corrected_text: str | None = None
    notes = ""

    if bucket == "markup_or_no_literal":
        decision, corrected_text = repair_markup_or_no(text)
        notes = "assisted markup/no literal triage"
    elif bucket == "spanish_lexical_residual":
        decision, corrected_text = repair_spanish_lexical(row, text)
        notes = "assisted Spanish lexical residual repair"
    elif bucket == "english_visible":
        decision, corrected_text = repair_english_visible(row, text)
        notes = "assisted English visible label repair"
    elif bucket == "mojibake_visible":
        decision = "false_positive_reopen"
        notes = "Portuguese accented uppercase text; not mojibake in rendered output"
    elif bucket == "dialogue_or_quote_surface":
        decision = classify_dialogue(text)
        notes = "quote/dialogue surface requires context composer"
    else:
        decision = "needs_domain_context"
        notes = "unrecognized residual bucket held for context"

    return {
        "queue_run_id": int(row["run_id"]),
        "queue_item_id": int(row["id"]),
        "ledger_item_id": int(row["ledger_item_id"]),
        "segment_id": int(row["segment_id"]),
        "decision": decision,
        "corrected_text": corrected_text or "",
        "notes": notes,
        "reviewer": REVIEWER,
    }


def write_outputs(
    *,
    decisions_path: Path,
    report_path: Path,
    queue_run_id: int,
    rows: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> None:
    counts = Counter(decision["decision"] for decision in decisions)
    bucket_counts = Counter(row["queue_bucket"] for row in rows)
    repair_by_bucket = Counter()
    for row, decision in zip(rows, decisions):
        if decision["decision"] == "needs_repair":
            repair_by_bucket[str(row.get("queue_bucket") or "")] += 1

    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for decision in decisions:
            handle.write(json.dumps(decision, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Short label single-issue residual assisted decisions",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Decision file: {decisions_path}",
        "",
        "Summary:",
        f"- Rows: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Queue buckets:",
        *[f"- {key}: {value:,}" for key, value in bucket_counts.most_common()],
        "",
        "Repair by bucket:",
        *[f"- {key}: {value:,}" for key, value in repair_by_bucket.most_common()],
        "",
        "Repair samples:",
    ]
    shown = 0
    for row, decision in zip(rows, decisions):
        if decision["decision"] != "needs_repair":
            continue
        shown += 1
        lines.extend(
            [
                f"- item={row['id']} segment={row['segment_id']} bucket={row['queue_bucket']} {row['relative_path']}::{row['source_key']}",
                f"  current: {short(row.get('confirmed_text') or row.get('evidence_text'))}",
                f"  repair:  {short(decision.get('corrected_text'))}",
            ]
        )
        if shown >= 40:
            break
    lines.extend(
        [
            "",
            "Safety note:",
            "- This script only writes a decision JSONL/report.",
            "- It does not ingest decisions, write output, create confirmations, or run production.",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int) -> dict[str, Any]:
    settings = db.load_settings()
    decisions_path, report_path = report_paths(settings, queue_run_id)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = fetch_queue_items(conn, queue_run_id)
    if not rows:
        raise RuntimeError(f"No queue items found for run {queue_run_id}.")
    decisions = [decide(row) for row in rows]
    write_outputs(
        decisions_path=decisions_path,
        report_path=report_path,
        queue_run_id=queue_run_id,
        rows=rows,
        decisions=decisions,
    )
    counts = Counter(decision["decision"] for decision in decisions)
    print("[short_label_residual_decisions] Decisions generated")
    print(f"[short_label_residual_decisions] Rule version: {RULE_VERSION}")
    print(f"[short_label_residual_decisions] Queue run id: {queue_run_id}")
    print(f"[short_label_residual_decisions] Rows: {len(rows)}")
    for key, value in counts.most_common():
        print(f"[short_label_residual_decisions] {key}: {value}")
    print(f"[short_label_residual_decisions] Decisions: {decisions_path}")
    print(f"[short_label_residual_decisions] Report: {report_path}")
    return {
        "queue_run_id": queue_run_id,
        "rows": len(rows),
        "counts": dict(counts),
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate assisted decisions for short-label residual queue.")
    parser.add_argument("--queue-run-id", type=int, required=True)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
