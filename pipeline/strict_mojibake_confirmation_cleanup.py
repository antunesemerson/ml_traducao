from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import audit_mojibake_confirmations
import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens
from apply_segment_state_updates import short


RULE_VERSION = "strict_mojibake_confirmation_cleanup_v1"
DEFAULT_BUCKET = "blocked_suspicious_confirmed_text"
RAW_PROTECTED_TOKEN_RE = re.compile(r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n")

STRICT_EXTRA_REPLACEMENTS = {
    '"? a lealdade': '"\u00c9 a lealdade',
    " ? minha frente": " \u00e0 minha frente",
    "? o qu\u00e3o": "\u00e9 o qu\u00e3o",
    "? sua origem": "\u00e0 sua origem",
    "? viagem": "\u00e0 viagem",
    "? ordem": "\u00e0 ordem",
    "? durante": "\u00c9 durante",
    "? uma verdadeira": "\u00c9 uma verdadeira",
    "H? certo": "H\u00e1 certo",
    "h? alguns meses": "h\u00e1 alguns meses",
    "h? muitas": "h\u00e1 muitas",
    "h? muitos": "h\u00e1 muitos",
    "h? muito tempo": "h\u00e1 muito tempo",
    "h? ningu\u00e9m": "h\u00e1 ningu\u00e9m",
    "h? outras": "h\u00e1 outras",
    "h? semanas": "h\u00e1 semanas",
    "h? s\u00e9culos": "h\u00e1 s\u00e9culos",
    "h? uma ninhada": "h\u00e1 uma ninhada",
    "Est? tudo": "Est\u00e1 tudo",
    "ajud?-lo": "ajud\u00e1-lo",
    "cultiv?-lo": "cultiv\u00e1-lo",
    "conhec?-l": "conhec\u00ea-l",
    "lev?-l": "lev\u00e1-l",
    "inspira??o": "inspira\u00e7\u00e3o",
    "po?tica": "po\u00e9tica",
    "ca?a": "ca\u00e7a",
    "ondula??es": "ondula\u00e7\u00f5es",
    "m?ximo": "m\u00e1ximo",
    "n?useas": "n\u00e1useas",
    "NÃO ?": "N\u00c3O \u00e9",
    "Não ?": "N\u00e3o \u00e9",
    "não ?": "n\u00e3o \u00e9",
}

# These fragments are too context-sensitive for automatic cleanup.
AMBIGUOUS_HITS = {
    " ? ",
    "? a",
    "? sua",
    "?s ",
    "?ndia",
    "?nico",
    "ter?",
    "dar?",
    "ficar?",
    "poder?",
    "tomar?",
    "ir?",
    "tr?s",
    "p?r",
}


def latest_policy_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_token_policy_runs
        WHERE finished_at IS NOT NULL
          AND total_candidates > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete segment_token_policy_runs entry found.")
    return int(row["id"])


def has_question_mojibake(value: str) -> bool:
    issues = local_quality_validator.validate_text(value)["issues"]
    return any(issue["code"] == "replacement_question_mark_mojibake" for issue in issues)


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def raw_protected_tokens(value: str | None) -> Counter[str]:
    if not value:
        return Counter()
    return Counter(RAW_PROTECTED_TOKEN_RE.findall(value))


def apply_replacements(text: str) -> tuple[str, list[str]]:
    updated = text
    hits: list[str] = []
    replacement_items = {
        **audit_mojibake_confirmations.REPLACEMENTS,
        **STRICT_EXTRA_REPLACEMENTS,
    }
    for before, after in sorted(replacement_items.items(), key=lambda item: len(item[0]), reverse=True):
        if before not in updated:
            continue
        updated = updated.replace(before, after)
        hits.append(before)
    return updated, hits


def classify_cleanup(original: str, fixed: str, hits: list[str]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if fixed == original:
        reasons.append("no_known_replacement")
        return "manual_review_no_known_fix", reasons
    ambiguous = sorted(hit for hit in hits if hit in AMBIGUOUS_HITS)
    if ambiguous:
        reasons.extend(f"ambiguous_hit:{hit}" for hit in ambiguous)
        return "manual_review_ambiguous_replacement", reasons
    if raw_protected_tokens(original) != raw_protected_tokens(fixed):
        reasons.append("raw_protected_tokens_changed")
        return "manual_review_token_literal_changed", reasons
    if protected_tokens(original) != protected_tokens(fixed):
        reasons.append("protected_tokens_changed")
        return "manual_review_token_changed", reasons
    if "?" in fixed:
        reasons.append("question_mark_remaining")
        return "manual_review_question_mark_remaining", reasons
    if has_question_mojibake(fixed):
        reasons.append("validator_still_flags_mojibake")
        return "manual_review_still_mojibake", reasons
    return "strict_fix_candidate", reasons


def fetch_rows(
    conn,
    *,
    policy_run_id: int,
    buckets: list[str],
    path_like: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    params: list[Any] = [policy_run_id]
    where = ["i.run_id = ?"]
    if buckets:
        placeholders = ", ".join("?" for _ in buckets)
        where.append(f"i.policy_bucket IN ({placeholders})")
        params.extend(buckets)
    if path_like:
        where.append("i.relative_path LIKE ?")
        params.append(path_like)
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            i.id AS policy_item_id,
            i.run_id AS policy_run_id,
            i.segment_id,
            i.relative_path,
            i.source_key,
            i.source_line_number,
            i.policy_bucket,
            i.risk_level,
            i.issue_flags_json,
            i.missing_tokens_json,
            i.extra_tokens_json,
            sc.id AS confirmation_id,
            sc.confirmed_text,
            sc.confirmation_level,
            sc.confirmation_source,
            sc.confirmation_label,
            sc.locked,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS output_text
        FROM segment_token_policy_items i
        JOIN source_segments s ON s.id = i.segment_id
        JOIN segment_confirmations sc ON sc.segment_id = i.segment_id
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        WHERE {" AND ".join(where)}
        ORDER BY
            CASE i.risk_level
                WHEN 'critical' THEN 0
                WHEN 'high' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low' THEN 3
                ELSE 9
            END,
            i.relative_path,
            i.source_line_number,
            i.segment_id
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def analyze_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    analyzed: list[dict[str, Any]] = []
    for row in rows:
        fixed, hits = apply_replacements(row["confirmed_text"] or "")
        cleanup_status, reasons = classify_cleanup(row["confirmed_text"] or "", fixed, hits)
        analyzed.append(
            {
                **row,
                "fixed_text": fixed,
                "replacement_hits": hits,
                "cleanup_status": cleanup_status,
                "cleanup_reasons": reasons,
            }
        )
    return analyzed


def write_outputs(
    settings: dict,
    *,
    policy_run_id: int,
    rows: list[dict[str, Any]],
    apply: bool,
) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{timestamp}_strict_mojibake_confirmation_cleanup"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    fieldnames = [
        "policy_item_id",
        "policy_run_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "policy_bucket",
        "risk_level",
        "cleanup_status",
        "cleanup_reasons",
        "replacement_hits",
        "applied",
        "confirmed_text",
        "fixed_text",
        "output_text",
        "english_text",
        "spanish_text",
        "old_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "policy_item_id": row["policy_item_id"],
                    "policy_run_id": row["policy_run_id"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_line_number": row["source_line_number"],
                    "source_key": row["source_key"],
                    "policy_bucket": row["policy_bucket"],
                    "risk_level": row["risk_level"],
                    "cleanup_status": row["cleanup_status"],
                    "cleanup_reasons": json.dumps(row["cleanup_reasons"], ensure_ascii=False),
                    "replacement_hits": json.dumps(row["replacement_hits"], ensure_ascii=False),
                    "applied": 1 if row.get("applied") else 0,
                    "confirmed_text": row["confirmed_text"],
                    "fixed_text": row["fixed_text"],
                    "output_text": row["output_text"],
                    "english_text": row["english_text"],
                    "spanish_text": row["spanish_text"],
                    "old_text": row["old_text"],
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    {
                        key: row.get(key)
                        for key in [
                            "policy_item_id",
                            "policy_run_id",
                            "segment_id",
                            "relative_path",
                            "source_line_number",
                            "source_key",
                            "policy_bucket",
                            "risk_level",
                            "cleanup_status",
                            "cleanup_reasons",
                            "replacement_hits",
                            "applied",
                            "confirmed_text",
                            "fixed_text",
                            "output_text",
                            "english_text",
                            "spanish_text",
                            "old_text",
                        ]
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    status_counts = Counter(row["cleanup_status"] for row in rows)
    hit_counts = Counter(hit for row in rows for hit in row["replacement_hits"])
    lines = [
        "Strict mojibake confirmation cleanup report",
        f"Rule version: {RULE_VERSION}",
        f"Policy run id: {policy_run_id}",
        f"Apply: {apply}",
        f"Rows inspected: {len(rows)}",
        "",
        "Cleanup status:",
        *[f"- {key}: {value}" for key, value in status_counts.most_common()],
        "",
        "Top replacement hits:",
        *[f"- {key}: {value}" for key, value in hit_counts.most_common(30)],
        "",
        "Preview:",
    ]
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- segment {row['segment_id']} | {row['cleanup_status']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']}"
                ),
                f"  hits: {', '.join(row['replacement_hits']) if row['replacement_hits'] else 'none'}",
                f"  reasons: {', '.join(row['cleanup_reasons']) if row['cleanup_reasons'] else 'none'}",
                f"  before: {short(row['confirmed_text'], 260)}",
                f"  after:  {short(row['fixed_text'], 260)}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def apply_candidates(conn, rows: list[dict[str, Any]]) -> int:
    now = db.utc_now()
    applied = 0
    for row in rows:
        row["applied"] = False
        if row["cleanup_status"] != "strict_fix_candidate":
            continue
        label = row["confirmation_label"] or ""
        if "strict_mojibake_fixed" not in label:
            label = (label + ";strict_mojibake_fixed").strip(";")
        conn.execute(
            """
            UPDATE segment_confirmations
            SET confirmed_text = ?,
                confirmation_label = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (row["fixed_text"], label, now, row["confirmation_id"]),
        )
        row["applied"] = True
        applied += 1
    return applied


def main(
    *,
    policy_run_id: int | None = None,
    buckets_csv: str | None = None,
    path_like: str | None = None,
    limit: int | None = None,
    apply: bool = False,
) -> None:
    settings = db.load_settings()
    buckets = split_csv(buckets_csv) or [DEFAULT_BUCKET]
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn)
        rows = fetch_rows(
            conn,
            policy_run_id=selected_policy_run_id,
            buckets=buckets,
            path_like=path_like,
            limit=limit,
        )
        analyzed = analyze_rows(rows)
        applied = 0
        if apply:
            applied = apply_candidates(conn, analyzed)
            conn.commit()
    txt_path, csv_path, jsonl_path = write_outputs(
        settings,
        policy_run_id=selected_policy_run_id,
        rows=analyzed,
        apply=apply,
    )

    status_counts = Counter(row["cleanup_status"] for row in analyzed)
    print("[strict_mojibake_confirmation_cleanup] Cleanup analyzed")
    print(f"[strict_mojibake_confirmation_cleanup] Rule version: {RULE_VERSION}")
    print(f"[strict_mojibake_confirmation_cleanup] Policy run id: {selected_policy_run_id}")
    print(f"[strict_mojibake_confirmation_cleanup] Rows inspected: {len(analyzed)}")
    for key, value in status_counts.most_common():
        print(f"[strict_mojibake_confirmation_cleanup] {key}: {value}")
    print(f"[strict_mojibake_confirmation_cleanup] Applied: {applied}")
    print(f"[strict_mojibake_confirmation_cleanup] Report: {txt_path}")
    print(f"[strict_mojibake_confirmation_cleanup] CSV: {csv_path}")
    print(f"[strict_mojibake_confirmation_cleanup] JSONL: {jsonl_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Strictly clean confirmed-text mojibake from token-policy buckets.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--buckets", default=None)
    parser.add_argument("--path-like", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(
        policy_run_id=args.policy_run_id,
        buckets_csv=args.buckets,
        path_like=args.path_like,
        limit=args.limit,
        apply=args.apply,
    )
