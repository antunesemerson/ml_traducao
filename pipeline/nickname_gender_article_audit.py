from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "nickname_gender_article_audit_v1"
NICKNAME_PATH = "nicknames_l_spanish.yml"

LINE_TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$\s]+\$|#[A-Za-z0-9_]+|#!")
ES_OA_RE = re.compile(r"\[[^\]]*Custom\(\s*['\"]ES_OA['\"]\s*\)[^\]]*\]", re.IGNORECASE)
ES_XA_RE = re.compile(r"\[[^\]]*Custom\(\s*['\"]ES_XA['\"]\s*\)[^\]]*\]", re.IGNORECASE)
ES_CUSTOM_RE = re.compile(r"Custom\(\s*['\"](ES_[A-Za-z0-9_]+)['\"]\s*\)", re.IGNORECASE)
SELECT_FEMALE_RE = re.compile(r"Select_CString\(\s*[^,]+\.IsFemale\s*,", re.IGNORECASE)
LITERAL_ARTICLE_PREFIX_RE = re.compile(r"^\s*(o/a|a/o|o\(a\)|a\(o\)|el/la|la/el)\b", re.IGNORECASE)
LITERAL_ARTICLE_ANY_RE = re.compile(r"\b(o/a|a/o|o\(a\)|a\(o\)|el/la|la/el)\b", re.IGNORECASE)
STATIC_ARTICLE_PREFIX_RE = re.compile(r"^\s*(o|a)\s+", re.IGNORECASE)
WORD_BEFORE_ES_OA_RE = re.compile(r"(?P<surface>[^\s\[\]]+)\s*\[CHARACTER\.Custom\('ES_OA'\)\]", re.IGNORECASE)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def latest_segment_state_run_id(conn) -> int | None:
    try:
        row = conn.execute(
            """
            SELECT id
            FROM segment_state_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    except Exception:
        return None
    return int(row["id"]) if row else None


def latest_segment_state(conn, segment_id: int, run_id: int | None) -> str | None:
    if run_id is None:
        return None
    try:
        row = conn.execute(
            """
            SELECT final_state
            FROM segment_state_items
            WHERE run_id = ?
              AND segment_id = ?
            LIMIT 1
            """,
            (run_id, segment_id),
        ).fetchone()
    except Exception:
        return None
    return row["final_state"] if row else None


def fetch_rows(conn, *, include_desc: bool) -> list[dict[str, Any]]:
    suffix_clause = "" if include_desc else "AND s.source_key NOT LIKE '%_desc'"
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.spanish_text,
            s.english_text,
            s.old_text,
            o.portuguese_text AS output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.is_active = 1
          AND s.relative_path = ?
          AND s.source_key LIKE 'nick_%'
          {suffix_clause}
        ORDER BY s.source_line_number, s.id
        """,
        (NICKNAME_PATH,),
    ).fetchall()
    return [dict(row) for row in rows]


def token_signature(text: str | None) -> list[str]:
    return LINE_TOKEN_RE.findall(text or "")


def es_custom_tokens(text: str | None) -> list[str]:
    return sorted(set(match.group(1) for match in ES_CUSTOM_RE.finditer(text or "")))


def has_probable_duplicate_oa_suffix(text: str) -> bool:
    for match in WORD_BEFORE_ES_OA_RE.finditer(text):
        surface = match.group("surface").strip()
        if not surface:
            continue
        if "/" in surface or "(" in surface or ")" in surface:
            return True
        if surface[-1:].lower() in {"o", "a"}:
            return True
    return False


def has_slash_or_parenthetical_gender_surface(text: str) -> bool:
    for match in WORD_BEFORE_ES_OA_RE.finditer(text):
        surface = match.group("surface")
        if "/" in surface or "(" in surface or ")" in surface:
            return True
    return False


def gender_surface_count(text: str) -> int:
    return len(WORD_BEFORE_ES_OA_RE.findall(text))


def classify(row: dict[str, Any]) -> tuple[str, list[str], str]:
    text = row.get("output_text") or ""
    key = str(row.get("source_key") or "")
    is_desc = key.endswith("_desc")
    reasons: list[str] = []

    literal_prefix = bool(LITERAL_ARTICLE_PREFIX_RE.search(text))
    literal_any = bool(LITERAL_ARTICLE_ANY_RE.search(text))
    static_article_prefix = bool(STATIC_ARTICLE_PREFIX_RE.search(text))
    has_es_oa = bool(ES_OA_RE.search(text))
    has_es_xa = bool(ES_XA_RE.search(text))
    has_select_female = bool(SELECT_FEMALE_RE.search(text))
    duplicate_suffix = has_probable_duplicate_oa_suffix(text)
    slash_surface = has_slash_or_parenthetical_gender_surface(text)
    surface_count = gender_surface_count(text)

    if literal_prefix:
        reasons.append("literal_article_prefix_visible")
    elif literal_any:
        reasons.append("literal_article_visible_inside_text")
    if static_article_prefix and (has_es_oa or has_es_xa):
        reasons.append("static_article_with_dynamic_gender_token")
    if has_es_oa:
        reasons.append("uses_ES_OA")
    if has_es_xa:
        reasons.append("uses_ES_XA")
    if duplicate_suffix:
        reasons.append("probable_duplicate_o_a_suffix")
    if slash_surface:
        reasons.append("slash_or_parenthetical_gender_surface")
    if surface_count > 1:
        reasons.append("compound_multiple_ES_OA_surfaces")
    if has_select_female:
        reasons.append("already_has_Select_CString_IsFemale")
    if row.get("old_text") and row.get("old_text") == text and (literal_prefix or duplicate_suffix or slash_surface):
        reasons.append("inherited_from_spanish_old")

    if is_desc:
        if duplicate_suffix or slash_surface:
            return "desc_gender_surface_review", reasons, "review_description_text"
        return "desc_out_of_scope_for_name_article", reasons, "no_action"

    if literal_prefix and has_es_oa:
        if duplicate_suffix or slash_surface:
            if surface_count > 1:
                return "needs_compound_article_and_gender_repair", reasons, "manual_or_controlled_composition"
            return "needs_article_and_gender_stem_repair", reasons, "controlled_repair"
        return "needs_dynamic_article_repair", reasons, "replace_literal_article_with_dynamic_article"

    if literal_prefix and has_es_xa:
        return "needs_dynamic_article_with_xa_review", reasons, "controlled_repair"

    if literal_prefix:
        return "needs_article_repair_or_invariant_review", reasons, "manual_or_controlled_composition"

    if static_article_prefix and has_es_oa:
        if duplicate_suffix or slash_surface:
            return "needs_static_article_and_gender_stem_repair", reasons, "controlled_repair"
        return "needs_static_article_dynamic_repair", reasons, "controlled_repair"

    if static_article_prefix and has_es_xa:
        return "needs_static_article_with_xa_review", reasons, "controlled_repair"

    if duplicate_suffix or slash_surface:
        return "needs_gender_stem_repair_without_literal_article", reasons, "controlled_repair"

    if has_select_female:
        return "already_dynamic_select", reasons, "no_action"

    return "no_literal_article_detected", reasons, "no_action"


def proposed_text_for_known_game_evidence(key: str) -> str | None:
    if key in {"nick_the_stammerer", "nick_the_stutterer"}:
        return "[Select_CString( CHARACTER.IsFemale, 'a Gaga', 'o Gago' )]"
    if key == "nick_the_lisp_and_lame":
        return "[Select_CString( CHARACTER.IsFemale, 'a Gaga e Coxa', 'o Gago e Coxo' )]"
    return None


def build_audit_rows(rows: list[dict[str, Any]], *, conn, segment_state_run_id: int | None) -> list[dict[str, Any]]:
    audited: list[dict[str, Any]] = []
    for row in rows:
        category, reasons, recommended_action = classify(row)
        output_text = row.get("output_text") or ""
        old_text = row.get("old_text") or ""
        proposed_text = proposed_text_for_known_game_evidence(str(row.get("source_key") or ""))
        audited.append(
            {
                "segment_id": row.get("segment_id"),
                "source_line_number": row.get("source_line_number"),
                "source_key": row.get("source_key"),
                "category": category,
                "recommended_action": recommended_action,
                "reasons": ";".join(reasons),
                "segment_state_run_id": segment_state_run_id,
                "segment_state": latest_segment_state(conn, int(row["segment_id"]), segment_state_run_id),
                "english_text": row.get("english_text") or "",
                "spanish_text": row.get("spanish_text") or "",
                "old_text": old_text,
                "output_text": output_text,
                "proposed_text": proposed_text or "",
                "output_tokens_json": json.dumps(token_signature(output_text), ensure_ascii=False),
                "source_tokens_json": json.dumps(token_signature(row.get("spanish_text") or ""), ensure_ascii=False),
                "es_custom_tokens_json": json.dumps(es_custom_tokens(output_text), ensure_ascii=False),
                "same_as_old": int(bool(old_text) and old_text == output_text),
            }
        )
    return audited


def write_reports(settings: dict[str, Any], audited: list[dict[str, Any]], *, include_desc: bool) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_nickname_gender_article_audit"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    category_counts = Counter(row["category"] for row in audited)
    action_counts = Counter(row["recommended_action"] for row in audited)
    reason_counts: Counter[str] = Counter()
    for row in audited:
        for reason in str(row["reasons"]).split(";"):
            if reason:
                reason_counts[reason] += 1

    fields = [
        "segment_id",
        "source_line_number",
        "source_key",
        "category",
        "recommended_action",
        "reasons",
        "segment_state_run_id",
        "segment_state",
        "english_text",
        "spanish_text",
        "old_text",
        "output_text",
        "proposed_text",
        "output_tokens_json",
        "source_tokens_json",
        "es_custom_tokens_json",
        "same_as_old",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(audited)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in audited:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Nickname gender/article audit",
        f"Rule version: {RULE_VERSION}",
        f"Include descriptions: {int(include_desc)}",
        f"Rows audited: {len(audited)}",
        "",
        "Category counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in category_counts.most_common())
    lines.extend(["", "Recommended action counts:"])
    lines.extend(f"- {key}: {value}" for key, value in action_counts.most_common())
    lines.extend(["", "Reason counts:"])
    lines.extend(f"- {key}: {value}" for key, value in reason_counts.most_common())
    lines.extend(["", "Known in-game evidence proposals:"])
    known = [row for row in audited if row["proposed_text"]]
    if known:
        for row in known:
            lines.append(
                f"- {row['source_key']} ({row['segment_id']}): {row['output_text']} -> {row['proposed_text']}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "Top actionable examples:"])
    actionable = [
        row
        for row in audited
        if row["recommended_action"] not in {"no_action"}
    ][:40]
    for row in actionable:
        lines.append(
            f"- {row['source_line_number']} {row['source_key']} [{row['category']}]: {row['output_text']}"
        )

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit CK3 nickname gender/article composition without applying changes.")
    parser.add_argument("--include-desc", action="store_true", help="Include nickname description rows in the audit.")
    args = parser.parse_args()

    settings = db.load_settings()
    with db.connect(settings) as conn:
        segment_state_run_id = latest_segment_state_run_id(conn)
        rows = fetch_rows(conn, include_desc=args.include_desc)
        audited = build_audit_rows(rows, conn=conn, segment_state_run_id=segment_state_run_id)
    txt_path, csv_path, jsonl_path = write_reports(settings, audited, include_desc=args.include_desc)
    print(f"Nickname gender/article audit complete: {len(audited)} rows")
    print(f"Report: {txt_path}")
    print(f"CSV: {csv_path}")
    print(f"JSONL: {jsonl_path}")


if __name__ == "__main__":
    main()
