from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib

import db
import local_quality_validator
import ml_score_segments
import quality_shadow_store
from apply_safe_output_updates import protected_tokens
from quality_missing_space_after_token_shadow import load_context_rows


RULE_VERSION = "quality_mojibake_lexicon_shadow_v5"
ISSUE_CODE = "replacement_question_mark_mojibake"
ELIGIBLE_LANE = "pairwise_evidence_eligible"
WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ?]+", re.UNICODE)
PORTUGUESE_REPAIR_CHARS = frozenset("áàâãéêíóôõúüç")
STRUCTURAL_LINE_BREAK_RE = re.compile(r"(?:\\n|\r?\n)")
STRUCTURAL_PARAGRAPH_BREAK_RE = re.compile(r"(?:(?:\\n){2,}|\r?\n(?:[ \t]*\r?\n)+)")
SPANISH_SENTENCE_START_COPULA_RE = re.compile(
    r"^\s*(?:[\\\"'«“¿]\s*)?"
    r"(?:(?:#\w+|#!|\$[^$]+\$|\[[^\]]+\])\s*)*"
    r"es\b",
    re.IGNORECASE,
)
STRUCTURAL_PREFIX_RE = re.compile(
    r"^(?:\s|[\\\"'«“¿]|#\w+|#!|\$[^$]+\$|\[[^\]]+\])*$"
)
SUPERVISED_PATTERN_MINIMUM = 2
DOMINANT_CANDIDATE_RATIO = 6.0


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def preview(value: Any, limit: int = 360) -> str:
    text = str(value or "").replace("\r", "").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue_codes(value: Any) -> set[str]:
    if not value:
        return set()
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return set()
    if not isinstance(parsed, list):
        return set()
    return {
        str(item.get("code") or item.get("issue_code"))
        for item in parsed
        if isinstance(item, dict) and (item.get("code") or item.get("issue_code"))
    }


def latest_full_output_score_run(conn: sqlite3.Connection, requested_id: int | None) -> dict[str, Any]:
    if requested_id is not None:
        row = conn.execute("SELECT * FROM ml_score_runs WHERE id = ?", (requested_id,)).fetchone()
    else:
        row = conn.execute(
            """
            SELECT *
            FROM ml_score_runs
            WHERE candidate_text_source = 'output'
              AND finished_at IS NOT NULL
              AND limit_count IS NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        raise RuntimeError("No completed full output score run was found.")
    result = dict(row)
    if str(result.get("candidate_text_source") or "") != "output":
        raise RuntimeError("Selected score run does not measure output text.")
    return result


def build_corpus_lexicon(conn: sqlite3.Connection) -> tuple[dict[int, set[str]], Counter[str]]:
    words_by_length: dict[int, set[str]] = defaultdict(set)
    support: Counter[str] = Counter()
    rows = conn.execute(
        """
        SELECT old_text AS text FROM source_segments WHERE is_active = 1 AND old_text IS NOT NULL
        UNION ALL
        SELECT portuguese_text AS text FROM output_segments WHERE portuguese_text IS NOT NULL
        UNION ALL
        SELECT confirmed_text AS text FROM segment_confirmations WHERE confirmed_text IS NOT NULL
        """
    )
    for row in rows:
        document_words = {
            match.group(0).casefold()
            for match in WORD_RE.finditer(str(row["text"] or ""))
            if "?" not in match.group(0) and len(match.group(0)) >= 1
        }
        for word in document_words:
            words_by_length[len(word)].add(word)
            support[word] += 1
    return words_by_length, support


def matches_pattern(pattern: str, candidate: str) -> bool:
    if len(pattern) != len(candidate):
        return False
    return all(expected == "?" or expected == actual for expected, actual in zip(pattern, candidate))


def restore_case(original: str, candidate: str) -> str:
    letters = [character for character in original if character.isalpha()]
    if len(letters) > 1 and all(character.isupper() for character in letters):
        return candidate.upper()
    if original[:1].isupper():
        return candidate[:1].upper() + candidate[1:]
    return candidate


def load_supervised_pattern_memory(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, int]]:
    table = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'mojibake_lexicon_replacement_decisions'
        """
    ).fetchone()
    if not table:
        return {}
    memory: dict[str, dict[str, int]] = defaultdict(dict)
    rows = conn.execute(
        """
        SELECT LOWER(original_text) AS original_text,
               LOWER(replacement_text) AS replacement_text,
               COUNT(DISTINCT segment_id) AS support
        FROM mojibake_lexicon_replacement_decisions
        WHERE outcome IN ('accepted_complete', 'accepted_partial')
        GROUP BY LOWER(original_text), LOWER(replacement_text)
        """
    ).fetchall()
    for row in rows:
        memory[str(row["original_text"] or "")][str(row["replacement_text"] or "")] = int(
            row["support"] or 0
        )
    return dict(memory)


def load_boundary_pattern_decisions(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    table = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'mojibake_boundary_pattern_decisions'
        """
    ).fetchone()
    if not table:
        return {}
    return {
        str(row["normalized_pattern"] or "").casefold(): dict(row)
        for row in conn.execute(
            """
            SELECT normalized_pattern, display_pattern, decision,
                   canonical_replacement, reason, reviewer,
                   source_shadow_run_id, source_score_run_id, updated_at
            FROM mojibake_boundary_pattern_decisions
            """
        ).fetchall()
        if str(row["normalized_pattern"] or "")
    }


def repaired_positions_are_portuguese(pattern: str, candidate: str) -> bool:
    positions = [index for index, character in enumerate(pattern) if character == "?"]
    return bool(positions) and all(
        candidate[index].casefold() in PORTUGUESE_REPAIR_CHARS
        for index in positions
    )


def accented_candidates(pattern: str, candidates: list[str]) -> list[str]:
    return [
        candidate
        for candidate in candidates
        if repaired_positions_are_portuguese(pattern, candidate)
    ]


def structural_paragraph_index(text: str, position: int) -> int:
    return sum(1 for _ in STRUCTURAL_PARAGRAPH_BREAK_RE.finditer(text[:position]))


def structural_paragraphs(text: str) -> list[str]:
    return STRUCTURAL_PARAGRAPH_BREAK_RE.split(str(text or ""))


def spanish_paragraph_supports_initial_e(
    output_text: str,
    spanish_text: str,
    position: int,
) -> bool:
    output_index = structural_paragraph_index(output_text, position)
    spanish_parts = structural_paragraphs(spanish_text)
    if output_index >= len(spanish_parts):
        return False
    paragraph = spanish_parts[output_index].replace("\\n", "\n")
    return bool(SPANISH_SENTENCE_START_COPULA_RE.search(paragraph))


def follows_structural_line_break(text: str, position: int) -> bool:
    prefix = text[:position]
    matches = list(STRUCTURAL_LINE_BREAK_RE.finditer(prefix))
    if not matches:
        return False
    return bool(STRUCTURAL_PREFIX_RE.fullmatch(prefix[matches[-1].end() :]))


def residual_question_mark_signals(text: str) -> list[str]:
    signals: list[str] = []
    for match in WORD_RE.finditer(text):
        token = match.group(0)
        if "?" not in token:
            continue
        if follows_separated_gender_plural_suffix(text, match):
            continue
        if (
            token.casefold() == "n?"
            and match.start() > 0
            and text[match.start() - 1] == "\\"
        ):
            signals.append("\\n?")
        else:
            signals.append(token)
    return signals


def follows_separated_gender_plural_suffix(
    text: str,
    match: re.Match[str],
) -> bool:
    if match.group(0).casefold() != "s?":
        return False
    structural_matches = list(
        local_quality_validator.GENDER_TOKEN_SEPARATED_PLURAL_SUFFIX_PATTERN.finditer(
            text[: match.end()]
        )
    )
    return bool(structural_matches) and structural_matches[-1].end() == match.start() + 1


def select_contextual_candidate(
    token: str,
    candidates: list[str],
    support: Counter[str],
    supervised_patterns: dict[str, dict[str, int]],
    boundary_decisions: dict[str, dict[str, Any]],
) -> tuple[str | None, str, float]:
    pattern = token.casefold()
    boundary_decision = boundary_decisions.get(pattern) or {}
    boundary_outcome = str(boundary_decision.get("decision") or "")
    if boundary_outcome == "canonical_replacement":
        selected = str(boundary_decision.get("canonical_replacement") or "").casefold()
        if (
            selected
            and matches_pattern(pattern, selected)
            and repaired_positions_are_portuguese(pattern, selected)
        ):
            return selected, "supervised_boundary_pattern", 0.99
        return None, "invalid_supervised_boundary_pattern", 0.0
    if boundary_outcome == "valid_punctuation":
        return None, "human_valid_punctuation", 0.0
    if boundary_outcome == "context_required":
        return None, "human_context_required", 0.0

    supervised = [
        (candidate, supervised_patterns.get(pattern, {}).get(candidate, 0))
        for candidate in candidates
        if supervised_patterns.get(pattern, {}).get(candidate, 0)
        >= SUPERVISED_PATTERN_MINIMUM
    ]
    supervised.sort(key=lambda item: (-item[1], -support[item[0]], item[0]))
    if supervised:
        selected, human_support = supervised[0]
        return selected, "supervised_pattern", min(0.99, 0.93 + human_support * 0.01)

    if pattern.startswith("?") or pattern.endswith("?"):
        return None, "boundary_question_mark_ambiguous", 0.0

    accented = accented_candidates(pattern, candidates)
    accented.sort(key=lambda candidate: (-support[candidate], candidate))
    if len(accented) == 1:
        return accented[0], "unique_portuguese_character", 0.93
    if len(accented) > 1:
        top = accented[0]
        runner_up = accented[1]
        top_support = int(support[top])
        runner_up_support = max(1, int(support[runner_up]))
        if (
            len(pattern) >= 3
            and top_support >= 10
            and top_support / runner_up_support >= DOMINANT_CANDIDATE_RATIO
        ):
            dominance = min(0.05, (top_support / runner_up_support) / 100)
            return top, "dominant_portuguese_character", 0.90 + dominance
        return None, "accented_candidate_ambiguous", 0.0
    return None, "portuguese_candidate_missing", 0.0


def repair_text_contextual(
    text: str,
    words_by_length: dict[int, set[str]],
    support: Counter[str],
    minimum_support: int,
    supervised_patterns: dict[str, dict[str, int]] | None = None,
    boundary_decisions: dict[str, dict[str, Any]] | None = None,
    english_text: str = "",
    spanish_text: str = "",
) -> tuple[str, list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    replacements: list[dict[str, Any]] = []
    blockers: list[str] = []
    unresolved: list[dict[str, Any]] = []
    supervised_patterns = supervised_patterns or {}
    boundary_decisions = boundary_decisions or {}

    def replace(match: re.Match[str]) -> str:
        raw_token = match.group(0)
        if "?" not in raw_token:
            return raw_token
        if follows_separated_gender_plural_suffix(text, match):
            blockers.append("gender_token_plural_suffix_spacing")
            unresolved.append(
                {
                    "token": "[token] s?",
                    "lexical_token": raw_token,
                    "reason": "gender_token_plural_suffix_spacing",
                    "candidate_count": 0,
                    "diagnostic_candidate_count": 0,
                    "alternatives": [],
                    "context_family": "gender_token_plural_suffix_spacing",
                    "aligned_source_evidence": "dynamic_gender_token_plural",
                }
            )
            return raw_token
        token = raw_token
        target_start = match.start()
        preserved_prefix = ""
        structural_newline = follows_structural_line_break(text, match.start())
        attached_structure_punctuation = bool(
            raw_token == "?"
            and match.start() > 0
            and text[match.start() - 1] in "]$!#"
        )
        if attached_structure_punctuation:
            structural_newline = False
        if (
            raw_token.casefold() == "n?"
            and match.start() > 0
            and text[match.start() - 1] == "\\"
        ):
            preserved_prefix = raw_token[0]
            token = raw_token[1:]
            target_start += 1
            structural_newline = True
        pattern = token.casefold()
        candidates = sorted(
            (
                word
                for word in words_by_length.get(len(pattern), set())
                if support[word] >= minimum_support and matches_pattern(pattern, word)
            ),
            key=lambda candidate: (-support[candidate], candidate),
        )
        relevant_candidates = sorted(
            accented_candidates(pattern, candidates),
            key=lambda candidate: (-support[candidate], candidate),
        )
        if attached_structure_punctuation:
            relevant_candidates = []
        context_family = (
            "newline_sentence_start"
            if token == "?" and structural_newline
            else "format_token_punctuation"
            if token == "?" and attached_structure_punctuation
            else "inline_singleton"
            if token == "?"
            else "lexical_accent_loss"
        )
        aligned_spanish_copula = bool(
            token == "?"
            and structural_newline
            and spanish_paragraph_supports_initial_e(text, spanish_text, target_start)
        )
        if aligned_spanish_copula:
            replacement = "É"
            replacements.append(
                {
                    "original": "?",
                    "replacement": replacement,
                    "start": target_start,
                    "end": target_start + 1,
                    "corpus_support": int(support["é"]),
                    "selection_reason": "structural_newline_aligned_spanish_copula",
                    "confidence": 0.98,
                    "candidate_count": 1,
                    "diagnostic_candidate_count": len(candidates),
                    "alternatives": [
                        {"text": "É", "corpus_support": int(support["é"])}
                    ],
                    "context_family": context_family,
                    "aligned_source_evidence": "spanish_sentence_start_es",
                }
            )
            return preserved_prefix + replacement
        selected, selection_reason, confidence = select_contextual_candidate(
            token,
            relevant_candidates,
            support,
            supervised_patterns,
            boundary_decisions,
        )
        if selected is None:
            blockers.append(selection_reason)
            display_token = (
                "\\n?"
                if structural_newline and token == "?"
                else "[estrutura]?"
                if attached_structure_punctuation
                else token
            )
            unresolved.append(
                {
                    "token": display_token,
                    "lexical_token": token,
                    "reason": selection_reason,
                    "candidate_count": len(relevant_candidates),
                    "diagnostic_candidate_count": len(candidates),
                    "alternatives": [
                        {"text": candidate, "corpus_support": int(support[candidate])}
                        for candidate in relevant_candidates[:8]
                    ],
                    "context_family": context_family,
                    "aligned_source_evidence": None,
                }
            )
            return raw_token
        replacement = restore_case(token, selected)
        replacements.append(
            {
                "original": token,
                "replacement": replacement,
                "start": target_start,
                "end": target_start + len(token),
                "corpus_support": int(support[selected]),
                "selection_reason": selection_reason,
                "confidence": round(confidence, 4),
                "candidate_count": len(relevant_candidates),
                "diagnostic_candidate_count": len(candidates),
                "alternatives": [
                    {"text": candidate, "corpus_support": int(support[candidate])}
                    for candidate in relevant_candidates[:8]
                ],
                "context_family": context_family,
                "aligned_source_evidence": None,
            }
        )
        return preserved_prefix + replacement

    candidate = WORD_RE.sub(replace, text)
    return candidate, replacements, blockers, unresolved


def repair_text(
    text: str,
    words_by_length: dict[int, set[str]],
    support: Counter[str],
    minimum_support: int,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    candidate, replacements, blockers, _ = repair_text_contextual(
        text,
        words_by_length,
        support,
        minimum_support,
    )
    return candidate, replacements, blockers


def load_score_rows(conn: sqlite3.Connection, score_run_id: int, threshold: float) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT score.*, source.relative_path, source.source_key,
                   source.english_text, source.spanish_text,
                   output.portuguese_text AS current_output_text,
                   COALESCE(confirmation.locked, 0) AS human_locked
            FROM ml_score_items score
            JOIN source_segments source ON source.id = score.segment_id
            JOIN output_segments output ON output.segment_id = score.segment_id
            LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = score.segment_id
            WHERE score.run_id = ?
              AND score.model_safe_probability < ?
              AND EXISTS (
                SELECT 1
                FROM json_each(score.issues_json) issue
                WHERE json_extract(issue.value, '$.code') = ?
              )
            ORDER BY score.model_safe_probability ASC, score.segment_id ASC
            """,
            (score_run_id, threshold, ISSUE_CODE),
        ).fetchall()
    ]


def build_records(
    conn: sqlite3.Connection,
    score_run: dict[str, Any],
    score_rows: list[dict[str, Any]],
    words_by_length: dict[int, set[str]],
    support: Counter[str],
    minimum_support: int,
    supervised_patterns: dict[str, dict[str, int]] | None = None,
    boundary_decisions: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    eligible_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    context_rows = load_context_rows(conn, [int(row["segment_id"]) for row in score_rows])
    for row in score_rows:
        original = str(row.get("candidate_text") or "")
        candidate, replacements, lexical_blockers, unresolved_signals = repair_text_contextual(
            original,
            words_by_length,
            support,
            minimum_support,
            supervised_patterns,
            boundary_decisions,
            str(row.get("english_text") or ""),
            str(row.get("spanish_text") or ""),
        )
        blockers = list(lexical_blockers)
        codes = issue_codes(row.get("issues_json"))
        if codes != {ISSUE_CODE}:
            blockers.append("other_issue_codes")
        if int(row.get("human_locked") or 0):
            blockers.append("human_locked_confirmation")
        if not replacements:
            blockers.append("no_supported_replacement")
        if candidate == original:
            blockers.append("no_change")
        if str(row.get("current_output_text") or "") != original:
            blockers.append("stale_output_text")
        token_ok = protected_tokens(original) == protected_tokens(candidate)
        if not token_ok:
            blockers.append("token_signature_changed")
        post_validation = local_quality_validator.validate_text(candidate)
        post_codes = [str(item.get("code")) for item in post_validation.get("issues") or []]
        if post_codes:
            blockers.append("post_validation_issue")
        residual_word_signals = residual_question_mark_signals(candidate)
        candidate_complete = (
            bool(replacements)
            and not residual_word_signals
            and not post_codes
            and token_ok
        )
        if residual_word_signals:
            blockers.append("residual_question_mark_signal")

        unique_blockers = sorted(set(blockers))
        eligible = not unique_blockers
        record = {
                "source": RULE_VERSION,
                "score_run_id": int(row["run_id"]),
                "model_run_id": int(score_run.get("model_run_id") or 0),
                "segment_id": int(row["segment_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "lane": ELIGIBLE_LANE if eligible else "blocked_or_context",
                "blockers": unique_blockers,
                "human_locked": bool(row.get("human_locked")),
                "original_preview": preview(original),
                "candidate_preview": preview(candidate),
                "baseline_hash": sha256_text(original),
                "candidate_hash": sha256_text(candidate),
                "replacements": replacements,
                "unresolved_signals": unresolved_signals,
                "residual_word_signals": residual_word_signals,
                "residual_signal_count": len(residual_word_signals),
                "candidate_complete": candidate_complete,
                "minimum_corpus_support": minimum_support,
                "pre_issue_codes": sorted(codes),
                "post_issue_codes": post_codes,
                "token_integrity_ok": token_ok,
                "raw_current_score": round(float(row.get("model_safe_probability") or 0), 6),
                "raw_candidate_score": None,
                "raw_score_delta": None,
                "calibrated_candidate_score": None,
                "calibrated_score_delta": None,
                "candidate_generation_only": True,
                "ready_for_apply": False,
                "output_changed": False,
            }
        records.append(record)
        if eligible:
            context = dict(context_rows.get(int(row["segment_id"])) or {})
            context.update(
                {
                    "candidate_text": candidate,
                    "candidate_text_source": RULE_VERSION,
                    "text_length": len(candidate),
                }
            )
            eligible_rows.append((row, context))

    if not eligible_rows:
        return records

    model_run_id = int(score_run.get("model_run_id") or 0)
    model_run = ml_score_segments.model_run_by_id(conn, model_run_id)
    bundle = joblib.load(db.project_path(model_run["model_path"]))
    model = bundle["model"]
    feature_set = bundle.get("metadata", {}).get("feature_set") or ml_score_segments.DEFAULT_FEATURE_SET
    safe_threshold = float(model_run.get("safe_threshold") or 0.90)
    predictions = ml_score_segments.model_predictions(
        model,
        [item[1] for item in eligible_rows],
        safe_threshold,
        feature_set,
    )
    scored: dict[int, dict[str, Any]] = {}
    for (score_row, scoring_row), prediction in zip(eligible_rows, predictions):
        model_action, raw_candidate_score, model_confidence, probabilities = prediction
        decision = ml_score_segments.final_decision(
            scoring_row,
            model_action,
            raw_candidate_score,
            model_confidence,
            probabilities,
            safe_threshold,
        )
        current_score = float(score_row.get("model_safe_probability") or 0.0)
        pairwise_score = min(1.0, max(float(raw_candidate_score), current_score + 0.02))
        scored[int(score_row["segment_id"])] = {
            "raw_candidate_score": round(float(raw_candidate_score), 6),
            "raw_score_delta": round(float(raw_candidate_score) - current_score, 6),
            "calibrated_candidate_score": round(pairwise_score, 6),
            "calibrated_score_delta": round(pairwise_score - current_score, 6),
            "calibration": "deterministic_mojibake_lexicon_pairwise_v1",
            "model_action_after": str(model_action),
            "final_action_after": str(decision["final_action"]),
            "model_confidence_after": round(float(model_confidence), 6),
        }
    for record in records:
        enrichment = scored.get(int(record["segment_id"]))
        if enrichment:
            record.update(enrichment)
    return records


def write_reports(
    settings: dict[str, Any],
    score_run: dict[str, Any],
    threshold: float,
    minimum_support: int,
    records: list[dict[str, Any]],
) -> dict[str, Path]:
    reports_dir = db.project_path(settings.get("reports_dir", "reports"))
    reports_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{stamp()}_quality_mojibake_lexicon_shadow"
    paths = {
        "markdown": reports_dir / f"{prefix}.md",
        "jsonl": reports_dir / f"{prefix}.jsonl",
        "summary": reports_dir / f"{prefix}_summary.json",
    }
    eligible = [record for record in records if record["lane"] == ELIGIBLE_LANE]
    complete_suggestions = [record for record in records if record.get("candidate_complete")]
    partial_suggestions = [
        record
        for record in records
        if record.get("replacements") and not record.get("candidate_complete")
    ]
    blocker_counts: Counter[str] = Counter(
        blocker for record in records for blocker in record["blockers"]
    )
    replacement_counts: Counter[str] = Counter(
        f"{item['original']} -> {item['replacement']}"
        for record in eligible
        for item in record["replacements"]
    )
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "score_run_id": int(score_run["id"]),
        "threshold": threshold,
        "minimum_corpus_support": minimum_support,
        "record_count": len(records),
        "pairwise_evidence_eligible_count": len(eligible),
        "complete_suggestion_count": len(complete_suggestions),
        "partial_suggestion_count": len(partial_suggestions),
        "blocked_count": len(records) - len(eligible),
        "blocker_counts": dict(blocker_counts),
        "eligible_replacement_counts": dict(replacement_counts),
        "pairwise_evidence_write_count": 0,
        "promotion_queue_write_count": 0,
        "apply_count": 0,
        "source_changed": False,
        "output_changed": False,
        "recommendation": (
            "Review the uniquely reconstructed lexical cohort as pairwise preference evidence. "
            "Keep ambiguous, unsupported, multi-issue and human-locked rows out of automatic promotion."
        ),
        "artifacts": {name: str(path) for name, path in paths.items()},
    }
    paths["jsonl"].write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    lines = [
        "# Quality Mojibake Lexicon Shadow",
        "",
        f"- Rule: `{RULE_VERSION}`",
        f"- Score run: `{score_run['id']}`",
        f"- Records: `{len(records)}`",
        f"- Pairwise eligible: `{len(eligible)}`",
        f"- Complete suggestions: `{len(complete_suggestions)}`",
        f"- Partial suggestions: `{len(partial_suggestions)}`",
        f"- Minimum corpus support: `{minimum_support}`",
        "- Writes: `0`",
        "",
        "## Blockers",
        "",
    ]
    lines.extend(f"- `{name}`: `{count}`" for name, count in blocker_counts.most_common())
    lines.extend(["", "## Eligible Replacements", ""])
    lines.extend(f"- `{name}`: `{count}`" for name, count in replacement_counts.most_common())
    lines.extend(["", "## Eligible Sample", ""])
    for record in eligible[:30]:
        lines.extend(
            [
                f"### Segment {record['segment_id']}",
                f"- `{record['relative_path']}::{record['source_key']}`",
                f"- Before: `{record['original_preview']}`",
                f"- After: `{record['candidate_preview']}`",
                f"- Replacements: `{json.dumps(record['replacements'], ensure_ascii=False)}`",
                "",
            ]
        )
    paths["markdown"].write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    paths["summary"].write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only lexical shadow for '?' mojibake repair.")
    parser.add_argument("--score-run-id", type=int, default=None)
    parser.add_argument("--threshold", type=float, default=0.50)
    parser.add_argument("--minimum-support", type=int, default=2)
    parser.add_argument("--persist-db", action="store_true")
    args = parser.parse_args()

    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=300)
    conn.row_factory = sqlite3.Row
    try:
        score_run = latest_full_output_score_run(conn, args.score_run_id)
        words_by_length, support = build_corpus_lexicon(conn)
        supervised_patterns = load_supervised_pattern_memory(conn)
        boundary_decisions = load_boundary_pattern_decisions(conn)
        score_rows = load_score_rows(conn, int(score_run["id"]), args.threshold)
        records = build_records(
            conn,
            score_run,
            score_rows,
            words_by_length,
            support,
            args.minimum_support,
            supervised_patterns,
            boundary_decisions,
        )
    finally:
        conn.close()

    paths = write_reports(
        settings, score_run, args.threshold, args.minimum_support, records
    )
    eligible_count = sum(record["lane"] == ELIGIBLE_LANE for record in records)
    complete_count = sum(bool(record.get("candidate_complete")) for record in records)
    partial_count = sum(
        bool(record.get("replacements")) and not record.get("candidate_complete")
        for record in records
    )
    shadow_snapshot = {}
    if args.persist_db:
        with db.connect(settings) as write_conn:
            db.ensure_database(write_conn)
            shadow_snapshot = quality_shadow_store.persist_snapshot(
                write_conn,
                source_rule_version=RULE_VERSION,
                score_run_id=int(score_run["id"]),
                records=records,
                eligible_lane=ELIGIBLE_LANE,
                metadata={
                    "threshold": args.threshold,
                    "minimum_support": args.minimum_support,
                    "supervised_pattern_count": len(supervised_patterns),
                    "boundary_pattern_decision_count": len(boundary_decisions),
                    "candidate_algorithm": "accent_filtered_structural_context_v4",
                },
            )
    print("[quality-mojibake-lexicon] Read-only shadow completed")
    print(f"[quality-mojibake-lexicon] Score run: {score_run['id']}")
    print(f"[quality-mojibake-lexicon] Records: {len(records)}")
    print(f"[quality-mojibake-lexicon] Pairwise eligible: {eligible_count}")
    for name, path in paths.items():
        print(f"[quality-mojibake-lexicon] {name.title()}: {path}")
    print(json.dumps({
        "schema_version": 1,
        "source": RULE_VERSION,
        "score_run_id": int(score_run["id"]),
        "record_count": len(records),
        "pairwise_evidence_eligible_count": eligible_count,
        "complete_suggestion_count": complete_count,
        "partial_suggestion_count": partial_count,
        **shadow_snapshot,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
