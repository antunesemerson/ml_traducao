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
import nickname_gender_article_audit as audit


RULE_VERSION = "nickname_gender_article_repair_dry_run_v2_strict"
LINGUISTIC_GUARD_VERSION = "nickname_gender_article_linguistic_guard_v1"
NICKNAME_PATH = audit.NICKNAME_PATH
READY_ARTICLE_ONLY = "ready_article_only"
READY_ARTICLE_ONLY_STRICT = "ready_article_only_strict"
READY_KNOWN_GAME_EVIDENCE_EXACT = "ready_known_game_evidence_exact"
CANDIDATE_STEM_REPAIR_REQUIRES_REVIEW = "candidate_stem_repair_requires_review"
CANDIDATE_ARTICLE_ONLY_NEEDS_LINGUISTIC_REVIEW = "candidate_article_only_needs_linguistic_review"
BLOCKED_SUSPECTED_SPANISH_OR_BAD_PT_ROOT = "blocked_suspected_spanish_or_bad_pt_root"
BLOCKED_INVARIANT_ADJECTIVE_OR_NOUN = "blocked_invariant_adjective_or_noun"
BLOCKED_NONSTANDARD_TOKEN = "blocked_invariant_or_nonstandard_gender_token"
BLOCKED_HUMAN_LOCKED = "blocked_human_locked"
BLOCKED_AMBIGUOUS_SURFACE = "blocked_ambiguous_surface"
BLOCKED_NOT_IN_SCOPE = "blocked_not_in_audit_scope"

ARTICLE_SELECT = "[Select_CString( CHARACTER.IsFemale, 'a', 'o' )]"
ALLOWED_GENDER_TOKENS = {"ES_OA", "ES_XA"}
NONSTANDARD_RE = re.compile(r"ES_[A-Za-z0-9_]+", re.IGNORECASE)
ES_OA_LITERAL_RE = re.compile(r"\[CHARACTER\.Custom\('ES_OA'\)\]", re.IGNORECASE)
ES_XA_LITERAL_RE = re.compile(r"\[CHARACTER\.Custom\('ES_XA'\)\]", re.IGNORECASE)
PREFIX_LITERAL_RE = re.compile(r"^\s*(?:o/a|a/o|o\(a\)|a\(o\)|el/la|la/el)\s+", re.IGNORECASE)
PREFIX_STATIC_RE = re.compile(r"^\s*(o|a)\s+", re.IGNORECASE)
WORD_BEFORE_ES_RE = re.compile(
    r"(?P<surface>[^\s\[\]]+)\s*\[CHARACTER\.Custom\('(?P<token>ES_OA|ES_XA)'\)\]",
    re.IGNORECASE,
)
ES_XA_PRODUCTIVE_SUFFIXES = ("or", "dor", "tor", "sor")
ES_OA_PRODUCTIVE_ROOT_SUFFIXES = (
    "ad",
    "id",
    "ud",
    "cad",
    "gad",
    "had",
    "lh",
    "nh",
    "c",
    "ic",
    "ific",
    "fic",
    "t",
    "it",
    "iv",
    "os",
    "eir",
    "eirad",
)
INVARIANT_OR_IRREGULAR_BASES = {
    "bom",
    "gentil",
    "habil",
    "hábil",
    "cruel",
    "resistente",
    "cambaleante",
    "viking",
    "opressor",
    "ressoador",
}
SUSPECT_SPANISH_OR_BAD_PT_BASES = {
    "pajarin",
    "blanc",
    "viej",
    "ented",
}


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def latest_state_run_id(conn) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def latest_confirmations(conn) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM segment_confirmations
        ORDER BY segment_id, updated_at DESC, confirmed_at DESC, id DESC
        """
    ).fetchall()
    latest: dict[int, dict[str, Any]] = {}
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id not in latest:
            latest[segment_id] = dict(row)
    return latest


def fetch_rows(conn, *, state_run_id: int | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.spanish_text,
            s.english_text,
            s.old_text,
            o.portuguese_text AS output_text,
            state.final_state AS segment_state,
            state.review_state,
            state.locked AS state_locked
        FROM source_segments s
        LEFT JOIN output_segments o
          ON o.segment_id = s.id
        LEFT JOIN segment_state_items state
          ON state.segment_id = s.id
         AND state.run_id = ?
        WHERE s.is_active = 1
          AND s.relative_path = ?
          AND s.source_key LIKE 'nick_%'
          AND s.source_key NOT LIKE '%_desc'
        ORDER BY s.source_line_number, s.id
        """,
        (state_run_id, NICKNAME_PATH),
    ).fetchall()
    return [dict(row) for row in rows]


def normalize_token_name(value: str) -> str:
    return value.upper()


def gender_tokens(text: str) -> list[str]:
    return sorted({normalize_token_name(match.group(0)) for match in NONSTANDARD_RE.finditer(text)})


def has_nonstandard_gender_token(text: str) -> bool:
    tokens = gender_tokens(text)
    return any(token not in ALLOWED_GENDER_TOKENS for token in tokens)


def has_ambiguous_surface(text: str) -> bool:
    if re.search(r"\b(?:o/a|a/o|o\(a\)|a\(o\)|el/la|la/el)\b", text, re.IGNORECASE):
        return False
    for match in WORD_BEFORE_ES_RE.finditer(text):
        surface = match.group("surface")
        if "/" in surface or "(" in surface or ")" in surface:
            return True
    return False


def has_ambiguous_gender_stem_surface(text: str) -> bool:
    for match in WORD_BEFORE_ES_RE.finditer(text):
        surface = match.group("surface")
        if "/" in surface or "(" in surface or ")" in surface:
            return True
    return False


def base_and_family_before_gender_token(text: str) -> tuple[str, str]:
    match = WORD_BEFORE_ES_RE.search(text)
    if not match:
        return "", ""
    return match.group("surface").strip(), match.group("token").upper()


def normalized_base(value: str) -> str:
    return value.strip().lower()


def strict_article_guard(text: str) -> tuple[str, str, str, str, int]:
    base, family = base_and_family_before_gender_token(text)
    base_key = normalized_base(base)
    if not base or not family:
        return (
            CANDIDATE_ARTICLE_ONLY_NEEDS_LINGUISTIC_REVIEW,
            "review",
            "missing_supported_gender_token_base",
            base,
            family,
            0,
        )
    if base_key in SUSPECT_SPANISH_OR_BAD_PT_BASES:
        return (
            BLOCKED_SUSPECTED_SPANISH_OR_BAD_PT_ROOT,
            "blocked",
            "suspected_spanish_or_bad_pt_root",
            base,
            family,
            0,
        )
    if base_key in INVARIANT_OR_IRREGULAR_BASES:
        return (
            BLOCKED_INVARIANT_ADJECTIVE_OR_NOUN,
            "blocked",
            "known_invariant_or_irregular_base",
            base,
            family,
            0,
        )
    if family == "ES_XA":
        if base_key.endswith(ES_XA_PRODUCTIVE_SUFFIXES):
            return (
                READY_ARTICLE_ONLY_STRICT,
                "ready",
                "xa_productive_suffix",
                base,
                family,
                1,
            )
        return (
            CANDIDATE_ARTICLE_ONLY_NEEDS_LINGUISTIC_REVIEW,
            "review",
            "xa_base_not_productive_or_suffix",
            base,
            family,
            0,
        )
    if family == "ES_OA":
        if base_key.endswith(ES_OA_PRODUCTIVE_ROOT_SUFFIXES):
            return (
                READY_ARTICLE_ONLY_STRICT,
                "ready",
                "oa_productive_root_suffix",
                base,
                family,
                1,
            )
        return (
            CANDIDATE_ARTICLE_ONLY_NEEDS_LINGUISTIC_REVIEW,
            "review",
            "oa_root_not_in_strict_productive_allowlist",
            base,
            family,
            0,
        )
    return (
        BLOCKED_NONSTANDARD_TOKEN,
        "blocked",
        "unsupported_gender_token_family",
        base,
        family,
        0,
    )


def stem_ends_with_o_or_a_before_es_oa(text: str) -> bool:
    for match in WORD_BEFORE_ES_RE.finditer(text):
        if match.group("token").upper() != "ES_OA":
            continue
        surface = match.group("surface").strip()
        if surface and surface[-1:].lower() in {"o", "a"}:
            return True
    return False


def suggest_stem_repair(text: str) -> str:
    proposed = dynamic_article_proposal(text)

    def repl(match: re.Match[str]) -> str:
        surface = match.group("surface")
        token = match.group("token")
        if token.upper() == "ES_OA" and surface[-1:].lower() in {"o", "a"}:
            return f"{surface[:-1]}[CHARACTER.Custom('ES_OA')]"
        return match.group(0)

    return WORD_BEFORE_ES_RE.sub(repl, proposed)


def dynamic_article_proposal(text: str) -> str:
    if PREFIX_LITERAL_RE.search(text):
        return PREFIX_LITERAL_RE.sub(f"{ARTICLE_SELECT} ", text, count=1)
    if PREFIX_STATIC_RE.search(text):
        return PREFIX_STATIC_RE.sub(f"{ARTICLE_SELECT} ", text, count=1)
    return text


def known_game_evidence_proposal(source_key: str) -> str | None:
    return audit.proposed_text_for_known_game_evidence(source_key)


def is_human_locked(row: dict[str, Any], confirmation: dict[str, Any] | None) -> bool:
    if row.get("segment_state") == "closed_human_locked":
        return True
    if int(row.get("state_locked") or 0) == 1 and row.get("review_state") == "human_locked":
        return True
    if confirmation and int(confirmation.get("locked") or 0) == 1:
        return True
    return False


def evaluate(row: dict[str, Any], *, confirmation: dict[str, Any] | None, state_run_id: int | None) -> dict[str, Any]:
    text = as_text(row.get("output_text"))
    source_key = as_text(row.get("source_key"))
    category, audit_reasons, _recommended_action = audit.classify(row)
    known_proposal = known_game_evidence_proposal(source_key)
    risks: list[str] = list(audit_reasons)
    ready = 0
    proposed = ""
    blocked_reason = ""
    repair_decision = BLOCKED_NOT_IN_SCOPE
    token_delta_kind = "none"
    in_audit_scope = bool(known_proposal) or category not in {
        "already_dynamic_select",
        "no_literal_article_detected",
    }

    if known_proposal:
        repair_decision = READY_KNOWN_GAME_EVIDENCE_EXACT
        ready = 1
        proposed = known_proposal
        token_delta_kind = "controlled_select_cstring_replacement"
        risks.append("known_game_evidence_exact")
    elif not in_audit_scope:
        blocked_reason = "no_article_gender_repair_candidate"
    elif is_human_locked(row, confirmation):
        repair_decision = BLOCKED_HUMAN_LOCKED
        blocked_reason = "segment_or_confirmation_human_locked"
    elif has_nonstandard_gender_token(text):
        repair_decision = BLOCKED_NONSTANDARD_TOKEN
        blocked_reason = "nonstandard_gender_token_requires_subpolicy"
        risks.append(f"gender_tokens={','.join(gender_tokens(text))}")
    elif has_ambiguous_gender_stem_surface(text):
        repair_decision = BLOCKED_AMBIGUOUS_SURFACE
        blocked_reason = "slash_or_parenthetical_gender_surface_requires_review"
    elif has_ambiguous_surface(text):
        repair_decision = BLOCKED_AMBIGUOUS_SURFACE
        blocked_reason = "slash_or_parenthetical_surface_requires_review"
    elif category in {
        "needs_dynamic_article_repair",
        "needs_static_article_dynamic_repair",
        "needs_dynamic_article_with_xa_review",
        "needs_static_article_with_xa_review",
    }:
        if stem_ends_with_o_or_a_before_es_oa(text):
            repair_decision = CANDIDATE_STEM_REPAIR_REQUIRES_REVIEW
            proposed = suggest_stem_repair(text)
            blocked_reason = "stem_ending_o_or_a_before_ES_OA_requires_review"
            token_delta_kind = "controlled_dynamic_article_plus_stem_suggestion"
        else:
            repair_decision = READY_ARTICLE_ONLY
            ready = 1
            proposed = dynamic_article_proposal(text)
            token_delta_kind = "controlled_dynamic_article_insert"
    elif category in {
        "needs_article_and_gender_stem_repair",
        "needs_static_article_and_gender_stem_repair",
        "needs_compound_article_and_gender_repair",
        "needs_gender_stem_repair_without_literal_article",
    }:
        repair_decision = CANDIDATE_STEM_REPAIR_REQUIRES_REVIEW
        proposed = suggest_stem_repair(text)
        blocked_reason = "stem_or_compound_gender_repair_requires_review"
        token_delta_kind = "controlled_dynamic_article_plus_stem_suggestion"
    elif category in {"needs_article_repair_or_invariant_review"}:
        repair_decision = BLOCKED_AMBIGUOUS_SURFACE
        proposed = dynamic_article_proposal(text)
        blocked_reason = "article_surface_without_supported_gender_token"
    elif repair_decision == BLOCKED_NOT_IN_SCOPE:
        blocked_reason = "no_article_gender_repair_candidate"

    if ready and proposed == text:
        ready = 0
        repair_decision = BLOCKED_NOT_IN_SCOPE
        blocked_reason = "proposal_equals_current_text"
        token_delta_kind = "none"

    legacy_repair_decision = repair_decision
    legacy_ready = ready
    strict_decision = repair_decision
    strict_ready = ready
    linguistic_guard_status = "not_applicable"
    linguistic_guard_reason = ""
    base_before_gender_token, gender_token_family = base_and_family_before_gender_token(text)

    if repair_decision == READY_ARTICLE_ONLY:
        (
            strict_decision,
            linguistic_guard_status,
            linguistic_guard_reason,
            base_before_gender_token,
            gender_token_family,
            strict_ready,
        ) = strict_article_guard(text)
        repair_decision = strict_decision
        ready = strict_ready
        if not strict_ready:
            blocked_reason = linguistic_guard_reason
            token_delta_kind = "none"
            risks.append(f"{LINGUISTIC_GUARD_VERSION}:{linguistic_guard_reason}")
        else:
            token_delta_kind = "controlled_dynamic_article_insert_strict"
    elif repair_decision == READY_KNOWN_GAME_EVIDENCE_EXACT:
        strict_decision = repair_decision
        strict_ready = 1
        linguistic_guard_status = "ready_known_game_evidence"
        linguistic_guard_reason = "known_game_evidence_exact_bypasses_article_guard"

    requires_human_review = int(repair_decision in {
        CANDIDATE_STEM_REPAIR_REQUIRES_REVIEW,
        CANDIDATE_ARTICLE_ONLY_NEEDS_LINGUISTIC_REVIEW,
        BLOCKED_SUSPECTED_SPANISH_OR_BAD_PT_ROOT,
        BLOCKED_INVARIANT_ADJECTIVE_OR_NOUN,
        BLOCKED_NONSTANDARD_TOKEN,
        BLOCKED_HUMAN_LOCKED,
        BLOCKED_AMBIGUOUS_SURFACE,
    })

    return {
        "segment_id": row.get("segment_id"),
        "source_line_number": row.get("source_line_number"),
        "source_key": source_key,
        "category": category,
        "legacy_repair_decision": legacy_repair_decision,
        "legacy_ready": legacy_ready,
        "repair_decision": repair_decision,
        "ready": ready,
        "strict_ready": strict_ready,
        "strict_decision": strict_decision,
        "linguistic_guard_status": linguistic_guard_status,
        "linguistic_guard_reason": linguistic_guard_reason,
        "base_before_gender_token": base_before_gender_token,
        "gender_token_family": gender_token_family,
        "blocked_reason": blocked_reason,
        "current_text": text,
        "proposed_text": proposed,
        "english_text": as_text(row.get("english_text")),
        "spanish_text": as_text(row.get("spanish_text")),
        "old_text": as_text(row.get("old_text")),
        "segment_state_run_id": state_run_id or "",
        "segment_state": as_text(row.get("segment_state")),
        "token_delta_kind": token_delta_kind,
        "risk_notes": ";".join(dict.fromkeys(note for note in risks if note)),
        "requires_human_review": requires_human_review,
        "confirmation_id": "" if confirmation is None else confirmation.get("id"),
        "confirmation_locked": 0 if confirmation is None else int(confirmation.get("locked") or 0),
    }


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_nickname_gender_article_repair_dry_run"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def write_reports(*, txt_path: Path, csv_path: Path, jsonl_path: Path, rows: list[dict[str, Any]]) -> None:
    decision_counts = Counter(row["repair_decision"] for row in rows)
    legacy_decision_counts = Counter(row["legacy_repair_decision"] for row in rows)
    category_counts = Counter(row["category"] for row in rows)
    blocked_counts = Counter(row["blocked_reason"] for row in rows if row["blocked_reason"])
    candidates = [row for row in rows if row["repair_decision"] != BLOCKED_NOT_IN_SCOPE]
    legacy_ready_rows = [row for row in rows if int(row["legacy_ready"] or 0) == 1]
    strict_ready_rows = [row for row in rows if int(row["strict_ready"] or 0) == 1]
    downgraded_rows = [
        row
        for row in rows
        if int(row["legacy_ready"] or 0) == 1 and int(row["strict_ready"] or 0) == 0
    ]
    gago_rows = [
        row
        for row in rows
        if row["source_key"] in {"nick_the_stammerer", "nick_the_stutterer", "nick_the_lisp_and_lame"}
    ]

    fields = [
        "segment_id",
        "source_line_number",
        "source_key",
        "category",
        "legacy_repair_decision",
        "legacy_ready",
        "repair_decision",
        "ready",
        "strict_ready",
        "strict_decision",
        "linguistic_guard_status",
        "linguistic_guard_reason",
        "base_before_gender_token",
        "gender_token_family",
        "blocked_reason",
        "current_text",
        "proposed_text",
        "english_text",
        "spanish_text",
        "old_text",
        "segment_state_run_id",
        "segment_state",
        "token_delta_kind",
        "risk_notes",
        "requires_human_review",
        "confirmation_id",
        "confirmation_locked",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Nickname gender/article repair dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Total nicknames audited: {len(rows):,}",
        f"Total candidates: {len(candidates):,}",
        f"legacy_ready_total: {len(legacy_ready_rows):,}",
        f"strict_ready_total: {len(strict_ready_rows):,}",
        f"strict_downgraded_from_ready: {len(downgraded_rows):,}",
        f"ready_known_game_evidence_exact: {decision_counts.get(READY_KNOWN_GAME_EVIDENCE_EXACT, 0):,}",
        f"ready_article_only_strict: {decision_counts.get(READY_ARTICLE_ONLY_STRICT, 0):,}",
        f"candidate_stem_repair_requires_review: {decision_counts.get(CANDIDATE_STEM_REPAIR_REQUIRES_REVIEW, 0):,}",
        f"candidate_article_only_needs_linguistic_review: {decision_counts.get(CANDIDATE_ARTICLE_ONLY_NEEDS_LINGUISTIC_REVIEW, 0):,}",
        f"blocked_suspected_spanish_or_bad_pt_root: {decision_counts.get(BLOCKED_SUSPECTED_SPANISH_OR_BAD_PT_ROOT, 0):,}",
        f"blocked_invariant_adjective_or_noun: {decision_counts.get(BLOCKED_INVARIANT_ADJECTIVE_OR_NOUN, 0):,}",
        f"blocked_invariant_or_nonstandard_gender_token: {decision_counts.get(BLOCKED_NONSTANDARD_TOKEN, 0):,}",
        f"blocked_human_locked: {decision_counts.get(BLOCKED_HUMAN_LOCKED, 0):,}",
        f"blocked_ambiguous_surface: {decision_counts.get(BLOCKED_AMBIGUOUS_SURFACE, 0):,}",
        f"blocked_not_in_audit_scope: {decision_counts.get(BLOCKED_NOT_IN_SCOPE, 0):,}",
        f"Ready total: {len(strict_ready_rows):,}",
        f"TXT: {txt_path}",
        f"CSV: {csv_path}",
        f"JSONL: {jsonl_path}",
        "",
        "Legacy decision counts:",
        *[f"- {key}: {value:,}" for key, value in legacy_decision_counts.most_common()],
        "",
        "Decision counts:",
        *[f"- {key}: {value:,}" for key, value in decision_counts.most_common()],
        "",
        "Category counts:",
        *[f"- {key}: {value:,}" for key, value in category_counts.most_common()],
        "",
        "Blocked reasons:",
        *[f"- {key}: {value:,}" for key, value in blocked_counts.most_common()],
        "",
        "Known Gago cases:",
    ]
    for row in gago_rows:
        lines.append(
            f"- {row['source_key']} ({row['segment_id']}): {row['current_text']} -> {row['proposed_text']} "
            f"[{row['repair_decision']}]"
        )

    lines.extend(["", "Downgraded from legacy ready:"])
    for row in downgraded_rows[:80]:
        lines.append(
            f"- line={row['source_line_number']} segment={row['segment_id']} {row['source_key']} | "
            f"{row['legacy_repair_decision']} -> {row['repair_decision']} | "
            f"base={row['base_before_gender_token']} {row['gender_token_family']} | "
            f"reason={row['linguistic_guard_reason']} | {row['current_text']}"
        )
    if len(downgraded_rows) > 80:
        lines.append(f"- ... {len(downgraded_rows) - 80:,} more downgraded rows in CSV/JSONL")

    lines.extend(["", "Top 40 actionable examples:"])
    for row in candidates[:40]:
        proposal = f" -> {row['proposed_text']}" if row["proposed_text"] else ""
        lines.append(
            f"- line={row['source_line_number']} segment={row['segment_id']} {row['source_key']} | "
            f"{row['repair_decision']} | ready={row['ready']} | {row['current_text']}{proposal}"
        )

    lines.extend(
        [
            "",
            "Safety note:",
            "- This dry-run is read-only.",
            "- It does not create confirmations.",
            "- It does not write output.",
            "- It does not reindex source files.",
            "- It does not modify source/ or output/.",
            "",
            "Recommendation:",
            "- Apply only after a protected apply prompt.",
            "- The 3 known Gago cases are the safest immediate production candidates.",
            "- The strict article-only set is promising, but should receive a final sampled review before apply.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        state_run_id = latest_state_run_id(conn)
        confirmations = latest_confirmations(conn)
        raw_rows = fetch_rows(conn, state_run_id=state_run_id)
        rows = [
            evaluate(row, confirmation=confirmations.get(int(row["segment_id"])), state_run_id=state_run_id)
            for row in raw_rows
        ]

    txt_path, csv_path, jsonl_path = report_paths(settings)
    write_reports(txt_path=txt_path, csv_path=csv_path, jsonl_path=jsonl_path, rows=rows)
    counts = Counter(row["repair_decision"] for row in rows)
    legacy_ready_total = sum(1 for row in rows if int(row["legacy_ready"] or 0) == 1)
    strict_ready_total = sum(1 for row in rows if int(row["strict_ready"] or 0) == 1)
    print("[nickname_gender_article_repair_dry_run] Dry-run generated")
    print(f"[nickname_gender_article_repair_dry_run] Rule version: {RULE_VERSION}")
    print(f"[nickname_gender_article_repair_dry_run] Total nicknames audited: {len(rows):,}")
    print(f"[nickname_gender_article_repair_dry_run] Total candidates: {sum(v for k, v in counts.items() if k != BLOCKED_NOT_IN_SCOPE):,}")
    print(f"[nickname_gender_article_repair_dry_run] Legacy ready total: {legacy_ready_total:,}")
    print(f"[nickname_gender_article_repair_dry_run] Strict ready total: {strict_ready_total:,}")
    print(f"[nickname_gender_article_repair_dry_run] Strict downgraded from ready: {legacy_ready_total - strict_ready_total:,}")
    for key, value in counts.most_common():
        print(f"[nickname_gender_article_repair_dry_run] {key}: {value:,}")
    print(f"[nickname_gender_article_repair_dry_run] Report: {txt_path}")
    print(f"[nickname_gender_article_repair_dry_run] CSV: {csv_path}")
    print(f"[nickname_gender_article_repair_dry_run] JSONL: {jsonl_path}")
    return {
        "total": len(rows),
        "counts": dict(counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only dry-run for CK3 nickname gender/article repair.")
    parser.parse_args()
    main()
