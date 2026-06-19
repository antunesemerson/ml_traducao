from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


RULE_VERSION = "nickname_gender_article_strict_review_v1"
REPORTS_DIR = Path("reports")
ARTICLE_SELECT_RE = re.compile(
    r"\[Select_CString\(\s*CHARACTER\.IsFemale\s*,\s*'a'\s*,\s*'o'\s*\)\]",
    re.IGNORECASE,
)
ES_OA_TOKEN = "[CHARACTER.Custom('ES_OA')]"
ES_XA_TOKEN = "[CHARACTER.Custom('ES_XA')]"
READY_DECISIONS = {
    "ready_article_only_strict",
    "ready_known_game_evidence_exact",
}

# These are not apply instructions. They are review notes for the current
# nickname article/gender pattern, kept conservative so production does not
# absorb linguistically weak forms as if they were fully learned.
MANUAL_DECISIONS: dict[str, tuple[str, str]] = {
    "nick_the_hermit": (
        "blocked_invariant_or_irregular_surface",
        "Eremita is common-gender in PT-BR; ES_OA renders male as Eremito.",
    ),
    "nick_the_deep_minded": (
        "semantic_review",
        "Douto/Douta may be acceptable, but it is not a direct article-only repair of Deep-Minded.",
    ),
    "nick_the_naysayer": (
        "semantic_review",
        "Negativo/Negativa is grammatically valid, but may be semantically weak for Naysayer.",
    ),
    "nick_the_zealot": (
        "semantic_review",
        "Fervoroso/Fervorosa is grammatical; keep for semantic review before broad policy.",
    ),
    "nick_the_campeador": (
        "semantic_review",
        "Campeador/Campeadora is grammatical, but this cultural title should remain review-gated.",
    ),
}


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def latest_repair_csv() -> Path:
    paths = sorted(
        REPORTS_DIR.glob("*nickname_gender_article_repair_dry_run.csv"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not paths:
        raise SystemExit("No nickname gender article repair dry-run CSV found.")
    return paths[0]


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def render_pt(text: str, *, female: bool) -> str:
    rendered = ARTICLE_SELECT_RE.sub("a" if female else "o", text)
    rendered = rendered.replace(ES_OA_TOKEN, "a" if female else "o")
    rendered = rendered.replace("[CHARACTER.Custom(\"ES_OA\")]", "a" if female else "o")
    rendered = rendered.replace(ES_XA_TOKEN, "a" if female else "")
    rendered = rendered.replace("[CHARACTER.Custom(\"ES_XA\")]", "a" if female else "")
    return re.sub(r"\s+", " ", rendered).strip()


def strict_ready(row: dict[str, str]) -> bool:
    if row.get("linguistic_guard_status") != "ready":
        return False
    if row.get("strict_ready") == "1":
        return True
    return row.get("strict_decision") in READY_DECISIONS


def classify(row: dict[str, str]) -> tuple[str, str]:
    key = row.get("source_key") or ""
    if key in MANUAL_DECISIONS:
        return MANUAL_DECISIONS[key]
    if row.get("strict_decision") == "ready_known_game_evidence_exact":
        return "safe_game_evidence", "validated by in-game Gago evidence policy"
    if row.get("category") in {
        "needs_static_article_with_xa_review",
        "needs_static_article_dynamic_repair",
    }:
        return "safe_static_article_repair", "static article replaced by dynamic article with supported gender token"
    if row.get("gender_token_family") == "ES_XA":
        return "safe_agentic_noun_gender", "productive ES_XA noun/adjective family with dynamic article"
    if row.get("gender_token_family") == "ES_OA":
        return "safe_article_only_es_oa", "productive ES_OA adjective family with dynamic article"
    return "review_unknown_family", "ready row has an unexpected gender token family"


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def reviewed_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        if not strict_ready(row):
            continue
        decision, review_note = classify(row)
        proposed_text = as_text(row.get("proposed_text"))
        output.append(
            {
                "segment_id": row.get("segment_id"),
                "source_line_number": row.get("source_line_number"),
                "source_key": row.get("source_key"),
                "english_text": row.get("english_text"),
                "category": row.get("category"),
                "strict_decision": row.get("strict_decision"),
                "gender_token_family": row.get("gender_token_family"),
                "segment_state": row.get("segment_state"),
                "current_text": row.get("current_text"),
                "proposed_text": proposed_text,
                "male_preview": render_pt(proposed_text, female=False),
                "female_preview": render_pt(proposed_text, female=True),
                "review_decision": decision,
                "review_note": review_note,
                "safe_for_batch_apply": int(decision.startswith("safe_")),
            }
        )
    return output


def write_reports(rows: list[dict[str, Any]], *, source_csv: Path, stamp: str) -> tuple[Path, Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    base = REPORTS_DIR / f"{stamp}_nickname_gender_article_strict_review"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    decision_counts = Counter(row["review_decision"] for row in rows)
    safe_count = sum(1 for row in rows if row["safe_for_batch_apply"])
    blocked_count = len(rows) - safe_count

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Nickname gender article strict review\n")
        handle.write(f"rule_version: {RULE_VERSION}\n")
        handle.write(f"source_csv: {source_csv}\n")
        handle.write(f"strict_ready_rows: {len(rows)}\n")
        handle.write(f"safe_for_batch_apply: {safe_count}\n")
        handle.write(f"held_for_review: {blocked_count}\n\n")
        handle.write("Decision counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nHeld for review:\n")
        for row in rows:
            if row["safe_for_batch_apply"]:
                continue
            handle.write(
                f"- {row['segment_id']} {row['source_key']}: "
                f"{row['english_text']} | M={row['male_preview']} | F={row['female_preview']} | "
                f"{row['review_decision']} | {row['review_note']}\n"
            )

    fieldnames = list(rows[0].keys()) if rows else []
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    return txt_path, csv_path, jsonl_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", type=Path, default=None)
    args = parser.parse_args()

    source_csv = args.input_csv or latest_repair_csv()
    rows = reviewed_rows(load_rows(source_csv))
    txt_path, csv_path, jsonl_path = write_reports(rows, source_csv=source_csv, stamp=now_stamp())

    decision_counts = Counter(row["review_decision"] for row in rows)
    print(f"source_csv: {source_csv}")
    print(f"strict_ready_rows: {len(rows)}")
    print(f"safe_for_batch_apply: {sum(1 for row in rows if row['safe_for_batch_apply'])}")
    print(f"held_for_review: {sum(1 for row in rows if not row['safe_for_batch_apply'])}")
    for decision, count in decision_counts.most_common():
        print(f"{decision}: {count}")
    print(f"txt_report: {txt_path}")
    print(f"csv_report: {csv_path}")
    print(f"jsonl_report: {jsonl_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
