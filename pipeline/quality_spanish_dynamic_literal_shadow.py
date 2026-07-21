from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib

import db
import local_quality_validator
import ml_score_segments
import quality_shadow_store
from apply_safe_output_updates import protected_tokens
from issue_dynamic_literal_repair_diagnostic import LITERAL_TRANSLATIONS, residual_hits
from offline_residual_proposals import (
    STRING_LITERAL_PATTERN,
    TOKEN_PATTERN,
    command_base_name,
    is_literal_translatable,
    token_status,
)
from quality_missing_space_after_token_shadow import load_context_rows
from quality_mojibake_lexicon_shadow import issue_codes, latest_full_output_score_run, preview


RULE_VERSION = "quality_spanish_dynamic_literal_shadow_v2"
ISSUE_CODE = "spanish_residue_in_literal"
ELIGIBLE_LANE = "pairwise_evidence_eligible"

# These surfaces are valid Portuguese in some contexts or need sentence-level
# composition. They remain visible in the shadow report, but never receive an
# automatic literal replacement from this provider.
CONTEXT_SENSITIVE_LITERALS = {
    "al",
    "del",
    "el",
    "ha",
    "has",
    "la",
    "las",
    "le",
    "los",
    "se ha",
    "sus",
    "te",
    "te has",
    "tu",
    "tus",
    "un",
    "una",
}

# High-confidence person-neutral pairs found repeatedly in Select_CString.
# Both branches collapse to the same PT-BR verb, so the local-player condition
# and every protected CK3 token remain unchanged.
EXTRA_SAFE_LITERAL_TRANSLATIONS = {
    "ayudaste": "ajudou",
    "ayudó": "ajudou",
    "comenzaste": "começou",
    "comenzó": "começou",
    "conservaste": "conservou",
    "conservó": "conservou",
    "conseguiste": "conseguiu",
    "consiguió": "conseguiu",
    "consolaste": "consolou",
    "consoló": "consolou",
    "contaste": "contou",
    "contó": "contou",
    "dejaste": "deixou",
    "dejó": "deixou",
    "demostraste": "demonstrou",
    "demostró": "demonstrou",
    "disfrutaste": "aproveitou",
    "disfrutó": "aproveitou",
    "diste": "deu",
    "dio": "deu",
    "decidí": "decidiu",
    "encerraste": "trancou",
    "encerró": "trancou",
    "intentaste": "tentou",
    "intentó": "tentou",
    "insultaste": "insultou",
    "insultó": "insultou",
    "mostraste": "mostrou",
    "mostró": "mostrou",
    "ofreciste": "ofereceu",
    "ofreció": "ofereceu",
    "pasaste": "passou",
    "pasó": "passou",
    "proclamaste": "proclamou",
    "proclamó": "proclamou",
    "quedaste": "ficou",
    "quedó": "ficou",
    "realizaste": "realizou",
    "realizó": "realizou",
    "recibiste": "recebeu",
    "recibió": "recebeu",
    "sigues": "continua",
    "sigue": "continua",
    "tuviste": "teve",
    "tuvo": "teve",
    "vasalla": "vassala",
    "vasallaje": "vassalagem",
    "vasallo": "vassalo",
}

SAFE_LITERAL_TARGETS = {
    " ".join(value.casefold().split())
    for value in (*LITERAL_TRANSLATIONS.values(), *EXTRA_SAFE_LITERAL_TRANSLATIONS.values())
}

# A translated dynamic verb immediately followed by another finite PT-BR verb
# usually means that the sentence was already composed outside the token. Only
# exact repetition is removed deterministically; different verbs stay in the
# diagnostic for a later sentence-composition provider.
FOLLOWING_FINITE_VERBS = {
    "adquiriu",
    "adora",
    "ama",
    "arruinou",
    "assassinou",
    "assistiu",
    "continua",
    "continuou",
    "deixou",
    "demonstrou",
    "deu",
    "está",
    "fez",
    "foi",
    "ganhou",
    "matou",
    "machucou",
    "mostrou",
    "passou",
    "tem",
    "teve",
    "venceu",
}
PRECEDING_INCOMPATIBLE_PREPOSITIONS = {"de", "para", "por", "sem"}
VISIBLE_CONTEXT_RESIDUAL_PATTERN = re.compile(r"\benojad[oa]?s?\b", re.IGNORECASE)

# Production scope: lexical substitutions plus past-tense mappings protected by
# sentence-composition guards. Other known mappings remain useful for
# investigation, but cannot enter the automatic promotion lifecycle yet.
PROMOTION_SAFE_LITERAL_SOURCES = {
    "ayudaste",
    "ayudó",
    "conseguiste",
    "consiguió",
    "consolaste",
    "consoló",
    "decidiste",
    "decidió",
    "dejaste",
    "dejó",
    "diste",
    "dio",
    "el señor",
    "encerraste",
    "encerró",
    "encontraste",
    "encontró",
    "eres",
    "es",
    "estás",
    "ganaste",
    "ganó",
    "ganará",
    "ganarás",
    "hiciste",
    "hizo",
    "intentaste",
    "intentó",
    "ofreciste",
    "ofreció",
    "pasaste",
    "pasó",
    "proclamaste",
    "realizaste",
    "realizó",
    "ti",
    "tú",
    "tuviste",
    "tuvo",
    "vasalla",
    "vasallaje",
    "vasallo",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def normalize_literal(value: str) -> str:
    return " ".join(value.casefold().split())


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def translated_literal(value: str) -> str | None:
    normalized = normalize_literal(value)
    if normalized in CONTEXT_SENSITIVE_LITERALS:
        return None
    translated = EXTRA_SAFE_LITERAL_TRANSLATIONS.get(normalized)
    if translated is None:
        translated = LITERAL_TRANSLATIONS.get(normalized)
    if translated is None:
        translated = LITERAL_TRANSLATIONS.get(strip_accents(normalized))
    if not translated or normalize_literal(translated) == normalized:
        return None
    if value.isupper():
        return translated.upper()
    if value[:1].isupper() and translated[:1].islower():
        return translated[:1].upper() + translated[1:]
    return translated


def repair_dynamic_literals(text: str) -> tuple[str, list[dict[str, Any]]]:
    parts: list[str] = []
    repairs: list[dict[str, Any]] = []
    position = 0
    for token_match in TOKEN_PATTERN.finditer(text):
        token = token_match.group(0)
        base_name = command_base_name(token)
        literal_index = 0
        token_repairs: list[dict[str, Any]] = []
        unresolved_literals: list[str] = []

        def replace_literal(match: Any) -> str:
            nonlocal literal_index
            literal_index += 1
            literal = match.group(1) if match.group(1) is not None else match.group(2)
            if literal is None or not is_literal_translatable(base_name, literal_index, literal):
                return match.group(0)
            translated = translated_literal(literal)
            if translated is None:
                normalized = normalize_literal(literal)
                if normalized and normalized not in SAFE_LITERAL_TARGETS:
                    unresolved_literals.append(literal)
                return match.group(0)
            quote = "'" if match.group(1) is not None else '"'
            token_repairs.append(
                {
                    "command": base_name,
                    "literal_index": literal_index,
                    "before": literal,
                    "after": translated,
                    "action": "translate_literal",
                }
            )
            return f"{quote}{translated}{quote}"

        repaired_token = STRING_LITERAL_PATTERN.sub(replace_literal, token)
        preceding = text[: token_match.start()].rstrip()
        preceding_word_match = re.search(r"([^\W\d_]+)$", preceding, re.UNICODE)
        preceding_word = normalize_literal(preceding_word_match.group(1)) if preceding_word_match else ""
        following = text[token_match.end() :].lstrip()
        following_words = [
            normalize_literal(value)
            for value in re.findall(r"[^\W\d_]+", following, re.UNICODE)[:2]
        ]
        following_word = following_words[0] if following_words else ""
        translated_values = {normalize_literal(item["after"]) for item in token_repairs}
        removed_redundant_token = False
        if token_repairs and not unresolved_literals and len(translated_values) == 1:
            translated_value = next(iter(translated_values))
            if following_word == translated_value:
                repaired_token = ""
                removed_redundant_token = True
                for item in token_repairs:
                    item["action"] = "remove_redundant_dynamic_literal"
        for item in token_repairs:
            item["unresolved_literals"] = sorted(set(unresolved_literals))
            item["preceding_word"] = preceding_word
            item["following_word"] = following_word
            item["following_words"] = following_words
        repairs.extend(token_repairs)
        parts.append(text[position : token_match.start()])
        parts.append(repaired_token)
        position = token_match.end()
        if (
            removed_redundant_token
            and token_match.start() > 0
            and text[token_match.start() - 1] in " \t"
            and position < len(text)
            and text[position] in " \t"
        ):
            position += 1
    parts.append(text[position:])
    return "".join(parts), repairs


def requires_sentence_composition(repair: dict[str, Any]) -> bool:
    if repair.get("action") == "remove_redundant_dynamic_literal":
        return False
    preceding_word = normalize_literal(str(repair.get("preceding_word") or ""))
    following_words = [
        normalize_literal(str(value))
        for value in repair.get("following_words") or []
        if str(value).strip()
    ]
    if not following_words:
        return True
    if preceding_word in PRECEDING_INCOMPATIBLE_PREPOSITIONS:
        return True
    if (
        preceding_word == "lhe"
        and normalize_literal(str(repair.get("after") or "")) == "fez"
        and following_words[0] in {"matar", "morrer"}
    ):
        return True
    if following_words[0] in FOLLOWING_FINITE_VERBS:
        return True
    return bool(
        following_words[0] == "se"
        and len(following_words) > 1
        and following_words[1] in FOLLOWING_FINITE_VERBS
    )


def load_score_rows(
    conn: sqlite3.Connection,
    score_run_id: int,
    threshold: float,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT score.*, source.relative_path, source.source_key,
                   source.spanish_text AS source_spanish_text,
                   output.portuguese_text AS current_output_text,
                   COALESCE(confirmation.locked, 0) AS human_locked
            FROM ml_score_items score
            JOIN source_segments source ON source.id = score.segment_id
            JOIN output_segments output ON output.segment_id = score.segment_id
            LEFT JOIN segment_confirmations confirmation
              ON confirmation.segment_id = score.segment_id
            WHERE score.run_id = ?
              AND score.model_safe_probability < ?
              AND EXISTS (
                SELECT 1 FROM json_each(score.issues_json) issue
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
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    eligible_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    context_rows = load_context_rows(conn, [int(row["segment_id"]) for row in score_rows])

    for row in score_rows:
        original = str(row.get("candidate_text") or "")
        candidate, repairs = repair_dynamic_literals(original)
        pre_codes = issue_codes(row.get("issues_json"))
        post_validation = local_quality_validator.validate_text(candidate)
        post_codes = sorted(
            {
                str(item.get("code"))
                for item in post_validation.get("issues") or []
                if item.get("code")
            }
        )
        token_ok = protected_tokens(original) == protected_tokens(candidate)
        source_status = token_status(str(row.get("source_spanish_text") or ""), candidate)
        blockers: list[str] = []
        if not repairs:
            blockers.append("no_trusted_literal_mapping")
        if candidate == original:
            blockers.append("no_change")
        if str(row.get("current_output_text") or "") != original:
            blockers.append("stale_output_text")
        if pre_codes != {ISSUE_CODE}:
            blockers.append("other_preexisting_issues")
        if repairs and any(
            normalize_literal(str(repair.get("before") or ""))
            not in PROMOTION_SAFE_LITERAL_SOURCES
            for repair in repairs
        ):
            blockers.append("mapping_requires_context_validation")
        if any(repair.get("unresolved_literals") for repair in repairs):
            blockers.append("partial_dynamic_literal_repair")
        if any(requires_sentence_composition(repair) for repair in repairs):
            blockers.append("sentence_composition_required")
        if bool(int(row.get("human_locked") or 0)):
            blockers.append("human_locked_confirmation")
        if not token_ok:
            blockers.append("token_signature_changed")
        if source_status == "mismatch":
            blockers.append("source_token_mismatch")
        if ISSUE_CODE in post_codes:
            blockers.append("spanish_literal_issue_remains")
        if set(post_codes) - {ISSUE_CODE}:
            blockers.append("post_validation_issue")
        if residual_hits(candidate) or VISIBLE_CONTEXT_RESIDUAL_PATTERN.search(candidate):
            blockers.append("residual_spanish_context_remains")

        unique_blockers = sorted(set(blockers))
        eligible = not unique_blockers
        record = {
            "source": RULE_VERSION,
            "score_run_id": int(row["run_id"]),
            "model_run_id": int(score_run.get("model_run_id") or 0),
            "segment_id": int(row["segment_id"]),
            "relative_path": str(row.get("relative_path") or ""),
            "source_key": str(row.get("source_key") or ""),
            "lane": ELIGIBLE_LANE if eligible else "blocked_or_context",
            "blockers": unique_blockers,
            "human_locked": bool(row.get("human_locked")),
            "original_preview": preview(original),
            "candidate_preview": preview(candidate),
            "repairs": repairs,
            "pre_issue_codes": sorted(pre_codes),
            "post_issue_codes": post_codes,
            "token_integrity_ok": token_ok,
            "source_token_status": source_status,
            "raw_current_score": round(float(row.get("model_safe_probability") or 0.0), 6),
            "raw_candidate_score": None,
            "raw_score_delta": None,
            "calibrated_candidate_score": None,
            "calibrated_score_delta": None,
            "candidate_generation_only": True,
            "ready_for_apply": False,
            "confirmation_write_count": 0,
            "output_write_count": 0,
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
            "calibration": "deterministic_spanish_dynamic_literal_pairwise_v1",
            "model_action_after": str(model_action),
            "final_action_after": str(decision["final_action"]),
            "model_confidence_after": round(float(model_confidence), 6),
        }
    for record in records:
        enrichment = scored.get(int(record["segment_id"]))
        if enrichment:
            record.update(enrichment)
    return records


def summarize_records(
    score_run: dict[str, Any],
    threshold: float,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    eligible = [record for record in records if record["lane"] == ELIGIBLE_LANE]
    blocker_counts = Counter(blocker for record in records for blocker in record["blockers"])
    repair_counts = Counter(
        f"{repair['before']} -> {repair['after']}"
        for record in eligible
        for repair in record["repairs"]
    )
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "score_run_id": int(score_run["id"]),
        "threshold": threshold,
        "record_count": len(records),
        "pairwise_evidence_eligible_count": len(eligible),
        "blocked_or_context_count": len(records) - len(eligible),
        "blocker_counts": dict(blocker_counts),
        "repair_counts": dict(repair_counts),
        "pairwise_evidence_write_count": 0,
        "promotion_queue_write_count": 0,
        "confirmation_write_count": 0,
        "output_write_count": 0,
        "artifacts": {},
    }


def write_reports(
    settings: dict[str, Any],
    score_run: dict[str, Any],
    threshold: float,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    reports_dir = db.project_path(settings.get("reports_dir", "reports"))
    reports_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{stamp()}_quality_spanish_dynamic_literal_shadow"
    paths = {
        "markdown": reports_dir / f"{prefix}.md",
        "jsonl": reports_dir / f"{prefix}.jsonl",
        "summary": reports_dir / f"{prefix}_summary.json",
    }
    eligible = [record for record in records if record["lane"] == ELIGIBLE_LANE]
    summary = summarize_records(score_run, threshold, records)
    blocker_counts = Counter(summary["blocker_counts"])
    repair_counts = Counter(summary["repair_counts"])
    with paths["jsonl"].open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["artifacts"] = {name: str(path) for name, path in paths.items()}
    paths["summary"].write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Shadow de espanhol residual em literais dinâmicos",
        "",
        f"- Regra: `{RULE_VERSION}`",
        f"- Score run: `{score_run['id']}`",
        f"- Registros com issue: `{len(records)}`",
        f"- Elegíveis para evidência pairwise: `{len(eligible)}`",
        f"- Bloqueados/contextuais: `{len(records) - len(eligible)}`",
        "- Escritas em confirmação/output: `0`",
        "",
        "## Bloqueios",
        "",
    ]
    lines.extend(f"- `{name}`: `{count}`" for name, count in blocker_counts.most_common())
    lines.extend(["", "## Reparos elegíveis", ""])
    lines.extend(f"- `{name}`: `{count}`" for name, count in repair_counts.most_common())
    lines.extend(["", "## Amostra", ""])
    for record in eligible[:50]:
        lines.append(
            f"- `{record['segment_id']}` · `{record['relative_path']}::{record['source_key']}` · "
            f"`{record['original_preview']}` → `{record['candidate_preview']}`"
        )
    paths["markdown"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a version-independent shadow for trusted Spanish dynamic-literal repairs."
    )
    parser.add_argument("--score-run-id", type=int)
    parser.add_argument("--threshold", type=float, default=0.50)
    parser.add_argument("--persist-db", action="store_true")
    parser.add_argument(
        "--report",
        action="store_true",
        help="Write optional report artifacts; the database snapshot remains authoritative.",
    )
    args = parser.parse_args()
    if not 0 < args.threshold <= 1:
        raise ValueError("threshold must be greater than zero and at most one")
    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=300) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        score_run = latest_full_output_score_run(conn, args.score_run_id)
        score_rows = load_score_rows(conn, int(score_run["id"]), args.threshold)
        records = build_records(conn, score_run, score_rows)
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
                metadata={"threshold": args.threshold},
            )
    summary = (
        write_reports(settings, score_run, args.threshold, records)
        if args.report
        else summarize_records(score_run, args.threshold, records)
    )
    summary.update(shadow_snapshot)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
