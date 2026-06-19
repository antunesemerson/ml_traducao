from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_gender_dynamic_token_delegate_diagnostic_v1"
DEFAULT_CLUSTER = "gender_dynamic_token_delegate"

GENDER_CUSTOM_RE = re.compile(r"\[[^\]]*\.Custom\(\s*['\"](ES_[A-Za-z]+)['\"]\s*\)[^\]]*\]")
SELECT_CSTRING_RE = re.compile(r"Select_CString\s*\(", re.IGNORECASE)
PREFIX_BEFORE_GENDER_RE = re.compile(
    r"\b(?:um|uma|o|a|do|da|ao|no|na|de|meu|minha|seu|sua|voss[oa]|"
    r"est[ae]|ess[ae]|aquel[ae]|outr[oa])\s*"
    r"\[[^\]]*\.Custom\(\s*['\"]ES_(?:XA|OA|EA|ElLa|DelDela|AlAla)['\"]",
    re.IGNORECASE,
)
TOKEN_THEN_VISIBLE_LETTER_RE = re.compile(
    r"\[[^\]]*\.Custom\(\s*['\"]ES_(?:OA|XA|EA)['\"]\s*\)[^\]]*\][A-Za-zÀ-ÿ]",
    re.IGNORECASE,
)
STEM_BEFORE_ESOA_RE = re.compile(
    r"([A-Za-zÀ-ÿ]{3,})\s*\[[^\]]*\.Custom\(\s*['\"]ES_OA['\"]\s*\)[^\]]*\]",
    re.IGNORECASE,
)
CONCEPT_GENDER_SUFFIX_RE = re.compile(
    r"Concept\([^)]*'[^']*(?:ad|aliad|inimig|pres|casad|noiv|convertid|favorit)[^']*'\s*\)\|E\]"
    r"\s*\[[^\]]*\.Custom\(\s*['\"]ES_OA['\"]",
    re.IGNORECASE,
)
RELATION_RE = re.compile(r"Custom2\(\s*['\"]RelationToMe(?:Short)?['\"]|GetAuntUncle|GetMotherFather", re.IGNORECASE)
LOCAL_PLAYER_RE = re.compile(r"LocalPlayerString|IsLocalPlayer", re.IGNORECASE)

SPANISH_LITERAL_MARKERS = (
    "amiga mia",
    "amiga mía",
    "amigo mio",
    "amigo mío",
    "autentica heroina",
    "autentico heroe",
    "auténtica heroína",
    "auténtico héroe",
    "de la buena",
    "del buen",
    "esta guerrera orgullosa",
    "este guerrero orgulloso",
    "eres",
    "ganaras",
    "ganarás",
    "ganara",
    "ganará",
    "intentaras",
    "intentarás",
    "intentara",
    "intentará",
    "heredaras",
    "heredarás",
    "heredara",
    "heredará",
    "la guerrera solitaria",
    "el guerrero solitario",
    "otro gorron",
    "otro gorrón",
    "otra gorrona",
    "señor",
    "señora",
    "misionera",
    "misionero",
    "cazadora",
    "cazador",
)
MOJIBAKE_MARKERS = ("Ã", "Â", "Ä", "�")
INVARIANT_ESOA_SURFACES = {
    "ciclope",
    "diplomata",
    "eremita",
    "livre",
    "jovem",
    "pobre",
    "nobre",
    "prudente",
    "inteligente",
}


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_gender_dynamic_token_delegate_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_cluster_files(reports_dir: Path, *, cluster: str, all_files: bool = False) -> list[Path]:
    files = sorted(reports_dir.glob(f"*_{cluster}.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    if all_files or not files:
        return files
    latest_date = max(path.name[:8] for path in files if path.name[:8].isdigit())
    return sorted([path for path in files if path.name.startswith(latest_date)])


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                rows.append(
                    {
                        "source_file": path.name,
                        "source_file_line": line_number,
                        "parse_error": "json_decode_error",
                    }
                )
                continue
            payload["source_file"] = path.name
            payload["source_file_line"] = line_number
            rows.append(payload)
    return rows


def nested(payload: dict[str, Any], *keys: str, default: Any = "") -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def lower_text(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in folded if not unicodedata.combining(char))


def has_spanish_literal(text: str) -> bool:
    low = lower_text(text)
    return any(marker in low for marker in SPANISH_LITERAL_MARKERS)


def has_mojibake(text: str) -> bool:
    return any(marker in text for marker in MOJIBAKE_MARKERS)


def gender_methods(text: str) -> list[str]:
    return sorted(set(GENDER_CUSTOM_RE.findall(text or "")))


def es_oa_surfaces(text: str) -> list[str]:
    return [match.group(1) for match in STEM_BEFORE_ESOA_RE.finditer(text or "")]


def evidence_issue_codes(queue: dict[str, Any]) -> list[str]:
    evidence = queue.get("evidence")
    if not isinstance(evidence, dict):
        return []
    codes = evidence.get("issue_codes")
    if not isinstance(codes, list):
        return []
    return sorted({str(code) for code in codes})


def classify_subpattern(row: dict[str, Any]) -> tuple[str, str, str, str]:
    queue = row.get("queue") if isinstance(row.get("queue"), dict) else {}
    decision = row.get("decision") if isinstance(row.get("decision"), dict) else {}
    texts = queue.get("texts") if isinstance(queue.get("texts"), dict) else {}

    confirmed = str(texts.get("confirmed_text") or texts.get("evidence_text") or "")
    english = str(texts.get("english_text") or "")
    issue_kind = str(queue.get("issue_kind") or "")
    queue_bucket = str(queue.get("queue_bucket") or "")
    decision_value = str(decision.get("decision") or "")
    issue_codes = evidence_issue_codes(queue)
    methods = gender_methods(confirmed)

    has_select = bool(SELECT_CSTRING_RE.search(confirmed))
    has_prefix = bool(PREFIX_BEFORE_GENDER_RE.search(confirmed))
    has_token_letter = bool(TOKEN_THEN_VISIBLE_LETTER_RE.search(confirmed))
    has_stem_esoa = bool(STEM_BEFORE_ESOA_RE.search(confirmed))
    has_relation = bool(RELATION_RE.search(confirmed))
    has_local_player = bool(LOCAL_PLAYER_RE.search(confirmed))
    es_oa_stems = es_oa_surfaces(confirmed)
    final_vowel_es_oa_stems = [
        stem for stem in es_oa_stems if stem[-1:].casefold() in {"a", "o"}
    ]
    invariant_es_oa_stems = [
        stem for stem in es_oa_stems if stem.casefold() in INVARIANT_ESOA_SURFACES
    ]
    text_length = int(nested(queue, "evidence", "text_length", default=len(confirmed)) or len(confirmed))
    token_count = int(nested(queue, "evidence", "token_count", default=len(methods)) or len(methods))
    word_count = int(nested(queue, "evidence", "word_count", default=0) or 0)

    if has_select and (has_spanish_literal(confirmed) or "spanish_residue_in_literal" in issue_codes):
        return (
            "select_cstring_spanish_literal_repair",
            "build_select_cstring_gender_literal_repair",
            "medium",
            "Select_CString gender literals still contain Spanish or inherited Spanish grammar.",
        )
    if has_token_letter or "missing_space_after_token" in issue_codes:
        return (
            "token_then_visible_letter_boundary_repair",
            "build_token_boundary_letter_repair",
            "medium",
            "A visible letter appears immediately after a CK3 gender helper token.",
        )
    if invariant_es_oa_stems:
        return (
            "invariant_word_before_es_oa_context",
            "route_invariant_gender_word_microagent",
            "low",
            "A common-gender or invariant PT-BR word is followed by ES_OA and needs lexical validation.",
        )
    if final_vowel_es_oa_stems:
        return (
            "stem_ending_o_or_a_before_es_oa_repair",
            "build_es_oa_final_vowel_trim_repair",
            "medium",
            "A word already ending in -o/-a is followed by ES_OA and may render doubled gender.",
        )
    if bool(CONCEPT_GENDER_SUFFIX_RE.search(confirmed)):
        return (
            "concept_gender_suffix_boundary_candidate",
            "build_concept_gender_suffix_boundary_bridge",
            "medium",
            "A CK3 Concept label is intentionally stemmed and completed by ES_OA.",
        )
    if has_relation or "RelationToMe" in english:
        return (
            "relation_possessive_dynamic_context",
            "route_relation_possessive_microagent",
            "low",
            "Relationship localization needs contextual wording, often avoiding meu/minha noise.",
        )
    if has_prefix and text_length <= 220 and token_count <= 5:
        return (
            "short_article_gender_boundary_candidate",
            "build_article_gender_boundary_false_reopen_bridge",
            "medium",
            "Short text uses visible article/preposition plus CK3 gender helper; often a false reopen.",
        )
    if has_prefix:
        return (
            "long_article_gender_boundary_context",
            "route_context_composer_with_gender_boundary",
            "low",
            "Article/preposition plus gender helper is embedded in longer prose and needs context.",
        )
    if "spanish_residue" in issue_codes or has_spanish_literal(confirmed) or has_mojibake(confirmed):
        return (
            "spanish_residual_with_gender_context",
            "route_spanish_residual_microagent",
            "low",
            "Gender-token segment also contains Spanish residue or mojibake; repair must happen before closure.",
        )
    if decision_value == "false_positive_reopen":
        return (
            "reviewed_false_positive_gender_reopen",
            "build_reviewed_false_reopen_bridge",
            "high",
            "Human/assisted review says the reopen is false and current text can likely remain closed.",
        )
    if has_stem_esoa and text_length <= 180:
        return (
            "short_productive_es_oa_suffix_candidate",
            "build_productive_es_oa_suffix_boundary_bridge",
            "medium",
            "Short productive adjective/noun stem completed by ES_OA.",
        )
    if token_count >= 8 or text_length >= 420 or word_count >= 55:
        return (
            "longform_gender_dynamic_context",
            "route_longform_context_composer",
            "low",
            "Long or token-dense prose with gender helpers should be fixed by composition, not a single rule.",
        )
    if methods:
        return (
            "generic_gender_dynamic_context",
            "route_gender_dynamic_manual_evidence",
            "low",
            "Gender helpers are present but no narrow safe subpattern was identified yet.",
        )
    return (
        "no_gender_marker_in_delegate_evidence",
        "audit_delegate_router",
        "low",
        "The delegate evidence did not expose a CK3 gender helper in confirmed text.",
    )


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    queue = row.get("queue") if isinstance(row.get("queue"), dict) else {}
    decision = row.get("decision") if isinstance(row.get("decision"), dict) else {}
    texts = queue.get("texts") if isinstance(queue.get("texts"), dict) else {}
    subpattern, next_action, leverage, rationale = classify_subpattern(row)
    confirmed = str(texts.get("confirmed_text") or texts.get("evidence_text") or "")
    methods = gender_methods(confirmed)
    issue_codes = evidence_issue_codes(queue)
    return {
        "source_file": row.get("source_file") or "",
        "source_file_line": row.get("source_file_line") or 0,
        "segment_id": int(queue.get("segment_id") or decision.get("segment_id") or 0),
        "relative_path": str(queue.get("relative_path") or ""),
        "source_key": str(queue.get("source_key") or ""),
        "source_line_number": int(queue.get("source_line_number") or 0),
        "queue_run_id": int(queue.get("queue_run_id") or decision.get("queue_run_id") or 0),
        "ledger_run_id": int(queue.get("ledger_run_id") or 0),
        "ledger_item_id": int(queue.get("ledger_item_id") or decision.get("ledger_item_id") or 0),
        "agent_key": str(queue.get("agent_key") or ""),
        "owner_agent": str(row.get("owner_agent") or ""),
        "decision": str(decision.get("decision") or ""),
        "next_action": next_action,
        "subpattern": subpattern,
        "leverage": leverage,
        "rationale": rationale,
        "issue_family": str(queue.get("issue_family") or ""),
        "issue_kind": str(queue.get("issue_kind") or ""),
        "queue_bucket": str(queue.get("queue_bucket") or ""),
        "domain": str(nested(queue, "evidence", "domain", default="")),
        "package": str(nested(queue, "evidence", "package", default="")),
        "policy_group": str(nested(queue, "evidence", "policy_group", default="")),
        "text_length": int(nested(queue, "evidence", "text_length", default=len(confirmed)) or len(confirmed)),
        "token_count": int(nested(queue, "evidence", "token_count", default=len(methods)) or len(methods)),
        "word_count": int(nested(queue, "evidence", "word_count", default=0) or 0),
        "gender_methods": ",".join(methods),
        "issue_codes": ",".join(issue_codes),
        "has_select_cstring": int(bool(SELECT_CSTRING_RE.search(confirmed))),
        "has_relation": int(bool(RELATION_RE.search(confirmed))),
        "has_prefix_before_gender": int(bool(PREFIX_BEFORE_GENDER_RE.search(confirmed))),
        "has_token_then_visible_letter": int(bool(TOKEN_THEN_VISIBLE_LETTER_RE.search(confirmed))),
        "has_spanish_literal": int(has_spanish_literal(confirmed)),
        "has_mojibake": int(has_mojibake(confirmed)),
        "confirmed_preview": short(confirmed, 260),
        "english_preview": short(str(texts.get("english_text") or ""), 220),
        "spanish_preview": short(str(texts.get("spanish_text") or ""), 220),
        "evidence_count": 1,
    }


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_dynamic_delegate_diagnostic_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            file_group TEXT NOT NULL,
            raw_evidence_count INTEGER NOT NULL DEFAULT 0,
            input_file_count INTEGER NOT NULL DEFAULT 0,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            high_leverage_count INTEGER NOT NULL DEFAULT 0,
            medium_leverage_count INTEGER NOT NULL DEFAULT 0,
            low_leverage_count INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS ml_issue_gender_dynamic_delegate_diagnostic_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source_file TEXT NOT NULL,
            source_file_line INTEGER NOT NULL DEFAULT 0,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER NOT NULL DEFAULT 0,
            queue_run_id INTEGER NOT NULL DEFAULT 0,
            ledger_run_id INTEGER NOT NULL DEFAULT 0,
            ledger_item_id INTEGER NOT NULL DEFAULT 0,
            agent_key TEXT,
            owner_agent TEXT,
            decision TEXT,
            subpattern TEXT NOT NULL,
            next_action TEXT NOT NULL,
            leverage TEXT NOT NULL,
            rationale TEXT,
            issue_family TEXT,
            issue_kind TEXT,
            queue_bucket TEXT,
            domain TEXT,
            package TEXT,
            policy_group TEXT,
            text_length INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            gender_methods TEXT,
            issue_codes TEXT,
            has_select_cstring INTEGER NOT NULL DEFAULT 0,
            has_relation INTEGER NOT NULL DEFAULT 0,
            has_prefix_before_gender INTEGER NOT NULL DEFAULT 0,
            has_token_then_visible_letter INTEGER NOT NULL DEFAULT 0,
            has_spanish_literal INTEGER NOT NULL DEFAULT 0,
            has_mojibake INTEGER NOT NULL DEFAULT 0,
            confirmed_preview TEXT,
            english_preview TEXT,
            spanish_preview TEXT,
            evidence_count INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_gender_dynamic_delegate_diagnostic_runs(id) ON DELETE CASCADE
        )
        """
    )
    db.ensure_columns(
        conn,
        "ml_issue_gender_dynamic_delegate_diagnostic_runs",
        [("raw_evidence_count", "INTEGER NOT NULL DEFAULT 0")],
    )
    db.ensure_columns(
        conn,
        "ml_issue_gender_dynamic_delegate_diagnostic_items",
        [("evidence_count", "INTEGER NOT NULL DEFAULT 1")],
    )


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    leverage_rank = {"high": 0, "medium": 1, "low": 2}
    grouped: dict[tuple[int, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (int(row["segment_id"]), row["subpattern"], row["next_action"])
        current = grouped.get(key)
        if current is None:
            grouped[key] = dict(row)
            continue
        current["evidence_count"] = int(current.get("evidence_count") or 1) + int(row.get("evidence_count") or 1)
        current_sources = {item for item in str(current.get("source_file") or "").split(";") if item}
        current_sources.add(str(row.get("source_file") or ""))
        current["source_file"] = ";".join(sorted(current_sources))
        if leverage_rank.get(row["leverage"], 9) < leverage_rank.get(current["leverage"], 9):
            replacement = dict(row)
            replacement["evidence_count"] = current["evidence_count"]
            replacement["source_file"] = current["source_file"]
            grouped[key] = replacement
    return list(grouped.values())


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    rows: list[dict[str, Any]],
    input_files: list[Path],
) -> None:
    fields = [
        "subpattern",
        "next_action",
        "leverage",
        "decision",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_run_id",
        "issue_kind",
        "queue_bucket",
        "domain",
        "package",
        "text_length",
        "token_count",
        "word_count",
        "gender_methods",
        "issue_codes",
        "has_select_cstring",
        "has_relation",
        "has_prefix_before_gender",
        "has_token_then_visible_letter",
        "has_spanish_literal",
        "has_mojibake",
        "confirmed_preview",
        "english_preview",
        "rationale",
        "evidence_count",
        "source_file",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    by_subpattern = Counter(row["subpattern"] for row in rows)
    by_action = Counter(row["next_action"] for row in rows)
    by_leverage = Counter(row["leverage"] for row in rows)
    by_decision = Counter(row["decision"] for row in rows)
    by_package = Counter(row["package"] for row in rows)
    unique_segments = len({row["segment_id"] for row in rows if row["segment_id"]})
    raw_evidence_count = sum(int(row.get("evidence_count") or 1) for row in rows)

    lines = [
        "Issue gender dynamic token delegate diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Inputs:",
        *[f"- {path.name}" for path in input_files],
        "",
        "Summary:",
        f"- Evidence rows after dedupe: {len(rows):,}",
        f"- Raw evidence rows: {raw_evidence_count:,}",
        f"- Unique segments: {unique_segments:,}",
        f"- High leverage: {by_leverage['high']:,}",
        f"- Medium leverage: {by_leverage['medium']:,}",
        f"- Low leverage/context: {by_leverage['low']:,}",
        "",
        "By subpattern:",
        *[f"- {key}: {value:,}" for key, value in by_subpattern.most_common()],
        "",
        "By next action:",
        *[f"- {key}: {value:,}" for key, value in by_action.most_common()],
        "",
        "By decision:",
        *[f"- {key}: {value:,}" for key, value in by_decision.most_common()],
        "",
        "Top packages:",
        *[f"- {key or '<none>'}: {value:,}" for key, value in by_package.most_common(12)],
        "",
        "Highest leverage samples:",
    ]
    samples = [row for row in rows if row["leverage"] in {"high", "medium"}][:40]
    if not samples:
        lines.append("- none")
    for row in samples:
        lines.extend(
            [
                (
                    f"- {row['subpattern']} | {row['next_action']} | "
                    f"segment={row['segment_id']} {row['relative_path']}::{row['source_key']}"
                ),
                f"  text: {row['confirmed_preview']}",
                f"  why: {row['rationale']}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Diagnostic only: no source/output writes, no confirmations, no production run.",
            "- Medium/high leverage means useful for a future subagent or lifecycle bridge, not automatic approval.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, files: list[Path] | None = None, all_files: bool = False) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    reports_dir = db.project_path(settings["reports_dir"])
    input_files = files or latest_cluster_files(reports_dir, cluster=DEFAULT_CLUSTER, all_files=all_files)
    if not input_files:
        raise RuntimeError(f"No {DEFAULT_CLUSTER!r} JSONL evidence files found in {reports_dir}")

    raw_rows: list[dict[str, Any]] = []
    for path in input_files:
        raw_rows.extend(read_jsonl(path))
    normalized_rows = [normalize_row(row) for row in raw_rows if not row.get("parse_error")]
    rows = dedupe_rows(normalized_rows)
    leverage_rank = {"high": 0, "medium": 1, "low": 2}
    rows.sort(
        key=lambda row: (
            leverage_rank.get(row["leverage"], 9),
            row["subpattern"],
            row["relative_path"],
            row["source_line_number"],
        )
    )

    txt_path, csv_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        now = datetime.now().isoformat(timespec="seconds")
        leverage = Counter(row["leverage"] for row in rows)
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_gender_dynamic_delegate_diagnostic_runs (
                rule_version,
                file_group,
                raw_evidence_count,
                input_file_count,
                candidate_count,
                high_leverage_count,
                medium_leverage_count,
                low_leverage_count,
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
                input_files[0].name[:8],
                len(normalized_rows),
                len(input_files),
                len(rows),
                int(leverage["high"]),
                int(leverage["medium"]),
                int(leverage["low"]),
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
            conn.execute(
                """
                INSERT INTO ml_issue_gender_dynamic_delegate_diagnostic_items (
                    run_id,
                    source_file,
                    source_file_line,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    queue_run_id,
                    ledger_run_id,
                    ledger_item_id,
                    agent_key,
                    owner_agent,
                    decision,
                    subpattern,
                    next_action,
                    leverage,
                    rationale,
                    issue_family,
                    issue_kind,
                    queue_bucket,
                    domain,
                    package,
                    policy_group,
                    text_length,
                    token_count,
                    word_count,
                    gender_methods,
                    issue_codes,
                    has_select_cstring,
                    has_relation,
                    has_prefix_before_gender,
                    has_token_then_visible_letter,
                    has_spanish_literal,
                    has_mojibake,
                    confirmed_preview,
                    english_preview,
                    spanish_preview,
                    evidence_count,
                    created_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    run_id,
                    row["source_file"],
                    row["source_file_line"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["queue_run_id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["agent_key"],
                    row["owner_agent"],
                    row["decision"],
                    row["subpattern"],
                    row["next_action"],
                    row["leverage"],
                    row["rationale"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["queue_bucket"],
                    row["domain"],
                    row["package"],
                    row["policy_group"],
                    row["text_length"],
                    row["token_count"],
                    row["word_count"],
                    row["gender_methods"],
                    row["issue_codes"],
                    row["has_select_cstring"],
                    row["has_relation"],
                    row["has_prefix_before_gender"],
                    row["has_token_then_visible_letter"],
                    row["has_spanish_literal"],
                    row["has_mojibake"],
                    row["confirmed_preview"],
                    row["english_preview"],
                    row["spanish_preview"],
                    row["evidence_count"],
                    now,
                ),
            )
        write_reports(txt_path=txt_path, csv_path=csv_path, jsonl_path=jsonl_path, run_id=run_id, rows=rows, input_files=input_files)
        conn.commit()

    print(f"Gender dynamic token delegate diagnostic run: {run_id}")
    print(f"Input files: {len(input_files)}")
    print(f"Raw rows: {len(normalized_rows)}")
    print(f"Rows after dedupe: {len(rows)}")
    print(f"Report: {txt_path}")
    return {
        "run_id": run_id,
        "input_file_count": len(input_files),
        "row_count": len(rows),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose gender dynamic token delegate evidence into subpatterns.")
    parser.add_argument("--file", action="append", default=[], help="Explicit JSONL evidence file path. Can be repeated.")
    parser.add_argument("--all-files", action="store_true", help="Use all matching gender_dynamic_token_delegate JSONL files.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    files = [Path(value) for value in args.file] if args.file else None
    main(files=files, all_files=args.all_files)
