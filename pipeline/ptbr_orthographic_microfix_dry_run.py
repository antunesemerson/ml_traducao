from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens
from local_quality_validator import validate_text


RULE_VERSION = "ptbr_orthographic_microfix_dry_run_v1"

REPLACEMENTS: tuple[tuple[str, str, str], ...] = (
    ("Só à possível", "Só é possível", "orthography_so_e_possivel"),
    ("estáo", "estão", "orthography_estao"),
    ("diféceis", "difíceis", "orthography_dificeis"),
    ("féceis", "fáceis", "orthography_faceis"),
    ("mátua", "mútua", "orthography_mutua"),
    ("culturas-máe", "culturas-mãe", "orthography_culturas_mae"),
    ("questáo", "questão", "orthography_questao"),
    ("gestáo", "gestão", "orthography_gestao"),
)

OTHER_VISIBLE_PROBLEM_PATTERNS: tuple[tuple[str, str], ...] = (
    ("seráo", "orthography_serao_not_in_phase_a_allowlist"),
    ("h?", "question_mark_mojibake_hint"),
    ("?", "question_mark_mojibake_or_unreviewed_question"),
    ("trein?-los", "question_mark_mojibake_treina_los"),
    ("vir?", "question_mark_mojibake_vira"),
    ("tirânico", "potential_unrelated_surface_issue"),
    ("tirÃºnico", "mojibake_tiranico"),
    ("à melhor", "crase_misuse_a_melhor"),
    ("à visto", "crase_misuse_a_visto"),
)

PROTECTED_TOKEN_RE = re.compile(r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n")


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def short(value: str | None, limit: int = 260) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def apply_outside_tokens(text: str) -> tuple[str, list[str], list[str]]:
    parts: list[str] = []
    hits: list[str] = []
    blocked_hits: list[str] = []
    last = 0
    for match in PROTECTED_TOKEN_RE.finditer(text):
        chunk = text[last : match.start()]
        chunk, chunk_hits = apply_plain_replacements(chunk)
        parts.append(chunk)
        token = match.group(0)
        token_after, token_hits = apply_plain_replacements(token)
        if token_hits and token_after != token:
            blocked_hits.extend(token_hits)
        parts.append(token)
        hits.extend(chunk_hits)
        last = match.end()
    chunk = text[last:]
    chunk, chunk_hits = apply_plain_replacements(chunk)
    parts.append(chunk)
    hits.extend(chunk_hits)
    return "".join(parts), hits, blocked_hits


def apply_plain_replacements(text: str) -> tuple[str, list[str]]:
    hits: list[str] = []
    result = text
    for old, new, label in REPLACEMENTS:
        if old in result:
            count = result.count(old)
            result = result.replace(old, new)
            hits.extend([label] * count)
    return result, hits


def fetch_candidates(conn) -> list[dict[str, Any]]:
    clauses = " OR ".join("o.portuguese_text LIKE ?" for _old, _new, _label in REPLACEMENTS)
    params = [f"%{old}%" for old, _new, _label in REPLACEMENTS]
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.is_active,
            o.output_line_number,
            o.portuguese_text AS output_text,
            o.output_raw_line
        FROM source_segments s
        JOIN output_segments o
          ON o.segment_id = s.id
        WHERE s.is_active = 1
          AND ({clauses})
        ORDER BY s.relative_path, o.output_line_number, s.source_key, s.id
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def file_current_text(output_root: Path, row: dict[str, Any]) -> tuple[str | None, str | None]:
    relative_path = as_text(row.get("relative_path"))
    line_number = row.get("output_line_number")
    if not relative_path or line_number is None:
        return None, None
    output_path = output_root / Path(relative_path)
    if not output_path.exists():
        return None, None
    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    line_index = int(line_number) - 1
    if line_index < 0 or line_index >= len(lines):
        return None, None
    raw_line = lines[line_index]
    first_quote = raw_line.find('"')
    last_quote = raw_line.rfind('"')
    if first_quote < 0 or last_quote <= first_quote:
        return raw_line, None
    return raw_line, raw_line[first_quote + 1 : last_quote].replace('\\"', '"')


def other_problem_hits(text: str, changed_labels: list[str]) -> list[str]:
    hits: list[str] = []
    for pattern, label in OTHER_VISIBLE_PROBLEM_PATTERNS:
        if pattern == "?":
            if "?" in text:
                hits.append(label)
            continue
        if pattern in text:
            hits.append(label)

    validation = validate_text(text)
    for issue in validation.get("issues") or []:
        code = as_text(issue.get("code"))
        severity = as_text(issue.get("severity"))
        if severity == "high" or code in {"question_mark_mojibake", "spanish_residue", "space_before_punctuation"}:
            hits.append(f"local_validator:{code}:{severity}")

    # "estáo" is intentionally allowed only when it is the only remaining accented-o typo after correction.
    if "orthography_estao" not in changed_labels and "estáo" in text:
        hits.append("orthography_estao_present_but_not_changed")
    return sorted(set(hits))


def classify(row: dict[str, Any], output_root: Path) -> dict[str, Any]:
    current = as_text(row["output_text"])
    corrected, replacement_hits, token_block_hits = apply_outside_tokens(current)
    reasons: list[str] = []
    category = "ready_exact_microfix"

    raw_line, file_text = file_current_text(output_root, row)
    if raw_line is None:
        reasons.append("missing_output_file_or_line")
    elif file_text is None:
        reasons.append("line_without_quoted_value")
    elif file_text != current:
        reasons.append("stale_file_output_text")

    if not replacement_hits:
        reasons.append("no_allowed_pattern_hit")
    if token_block_hits:
        reasons.append("replacement_inside_protected_token:" + ",".join(sorted(set(token_block_hits))))
    if protected_tokens(current) != protected_tokens(corrected):
        reasons.append("protected_tokens_changed")
    if current == corrected:
        reasons.append("no_text_delta")

    if reasons:
        category = "blocked_structural_or_token_risk"
    else:
        review_hits = other_problem_hits(corrected, replacement_hits)
        if review_hits:
            category = "candidate_multi_issue_requires_review"
            reasons.extend(review_hits)

    return {
        "segment_id": row["segment_id"],
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "output_line_number": row["output_line_number"],
        "category": category,
        "replacements": ",".join(sorted(Counter(replacement_hits).elements())),
        "replacement_count": len(replacement_hits),
        "reasons": ";".join(reasons),
        "current_text": current,
        "corrected_text": corrected,
        "tokens_before": json.dumps(protected_tokens(current), ensure_ascii=False, sort_keys=True),
        "tokens_after": json.dumps(protected_tokens(corrected), ensure_ascii=False, sort_keys=True),
    }


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_ptbr_orthographic_microfix_dry_run"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def write_reports(
    settings: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    pattern_counts: Counter[str],
    category_counts: Counter[str],
) -> tuple[Path, Path, Path]:
    txt_path, csv_path, jsonl_path = report_paths(settings)
    fields = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "output_line_number",
        "category",
        "replacements",
        "replacement_count",
        "reasons",
        "current_text",
        "corrected_text",
        "tokens_before",
        "tokens_after",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    ready = [row for row in rows if row["category"] == "ready_exact_microfix"]
    retained = [row for row in rows if row["category"] != "ready_exact_microfix"]
    lines = [
        "PT-BR orthographic microfix dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "Mode: dry_run/read_only",
        "",
        "Pattern hits:",
    ]
    for old, _new, label in REPLACEMENTS:
        lines.append(f"- {label} ({old!r}): {pattern_counts[label]:,}")
    lines.extend(
        [
            "",
            "Summary:",
            f"- inspected_pattern_hits: {sum(pattern_counts.values()):,}",
            f"- distinct_candidate_segments: {len(rows):,}",
            f"- ready_exact_microfix: {category_counts['ready_exact_microfix']:,}",
            f"- candidate_multi_issue_requires_review: {category_counts['candidate_multi_issue_requires_review']:,}",
            f"- blocked_structural_or_token_risk: {category_counts['blocked_structural_or_token_risk']:,}",
            "- applied: 0",
            "- confirmation_created: 0",
            "- output_written: 0",
            "- files_touched: 0",
            f"- TXT: {txt_path}",
            f"- CSV: {csv_path}",
            f"- JSONL: {jsonl_path}",
            "",
            "Ready segments:",
        ]
    )
    for row in ready:
        lines.append(
            f"- {row['segment_id']} | {row['relative_path']}:{row['output_line_number']} | "
            f"{row['source_key']} | replacements={row['replacements']}"
        )
    lines.extend(["", "Retained/blocked segments:"])
    for row in retained:
        lines.append(
            f"- {row['segment_id']} | {row['category']} | {row['relative_path']}:{row['output_line_number']} | "
            f"{row['source_key']} | reasons={row['reasons'] or 'none'}"
        )
    lines.extend(["", "Samples:"])
    for row in rows[:35]:
        lines.extend(
            [
                f"- segment={row['segment_id']} | {row['category']} | {row['source_key']}",
                f"  before: {short(row['current_text'])}",
                f"  after:  {short(row['corrected_text'])}",
                f"  reasons: {row['reasons'] or 'none'}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This is a read-only dry-run.",
            "- It does not create confirmations and does not write output/source.",
            "- Fase A intentionally does not run segment-state.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main() -> dict[str, Any]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    with db.connect(settings) as conn:
        rows = fetch_candidates(conn)

    classified = [classify(row, output_root) for row in rows]
    pattern_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    for row in classified:
        category_counts[row["category"]] += 1
        for label in filter(None, row["replacements"].split(",")):
            pattern_counts[label] += 1

    txt_path, csv_path, jsonl_path = write_reports(
        settings,
        rows=classified,
        pattern_counts=pattern_counts,
        category_counts=category_counts,
    )

    print("[ptbr_orthographic_microfix_dry_run] Done")
    print(f"[ptbr_orthographic_microfix_dry_run] Rule version: {RULE_VERSION}")
    print(f"[ptbr_orthographic_microfix_dry_run] Distinct candidate segments: {len(classified)}")
    print(f"[ptbr_orthographic_microfix_dry_run] Ready exact microfix: {category_counts['ready_exact_microfix']}")
    print(
        "[ptbr_orthographic_microfix_dry_run] Candidate multi-issue requires review: "
        f"{category_counts['candidate_multi_issue_requires_review']}"
    )
    print(
        "[ptbr_orthographic_microfix_dry_run] Blocked structural/token risk: "
        f"{category_counts['blocked_structural_or_token_risk']}"
    )
    print("[ptbr_orthographic_microfix_dry_run] Applied: 0")
    print("[ptbr_orthographic_microfix_dry_run] Confirmation created: 0")
    print("[ptbr_orthographic_microfix_dry_run] Output written: 0")
    print("[ptbr_orthographic_microfix_dry_run] Files touched: 0")
    print(f"[ptbr_orthographic_microfix_dry_run] TXT: {txt_path}")
    print(f"[ptbr_orthographic_microfix_dry_run] CSV: {csv_path}")
    print(f"[ptbr_orthographic_microfix_dry_run] JSONL: {jsonl_path}")
    return {
        "counts": dict(category_counts),
        "txt_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    main()
