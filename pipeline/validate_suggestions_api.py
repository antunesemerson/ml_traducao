from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import db


RULE_VERSION = "api_review_v3"
DEFAULT_LIMIT = 25
TARGET_DECISIONS = ("accepted", "rejected", "edited", "accepted_old")
PROTECTED_TOKEN_PATTERN = re.compile(
    r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n"
)
STRING_LITERAL_PATTERN = re.compile(r"'[^']*'|\"[^\"]*\"")
SPANISH_ANGULAR_QUOTE_MARKS = ("«", "»", "Â«", "Â»")
PERSISTENT_SPANISH_RESIDUES = {
    "cortesano",
    "cortesanos",
    "decisiones",
    "decisión",
    "gobernante",
    "gobernantes",
    "hacendado",
    "hacendados",
    "invitado",
    "invitados",
    "nueva",
    "nuevas",
    "rechaza",
    "rechazar",
    "situación",
    "situaciones",
}


REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "string",
            "enum": list(TARGET_DECISIONS),
        },
        "corrected_text": {
            "type": "string",
            "description": "Final corrected pt-BR text when decision is edited; otherwise empty.",
        },
        "confidence_score": {
            "type": "number",
            "description": "Confidence from 0.0 to 1.0 that this decision can be applied without human edits.",
        },
        "reason": {
            "type": "string",
            "description": "Short reason in Brazilian Portuguese.",
        },
        "detected_issues": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "none",
                    "spanish_residue",
                    "wrong_meaning",
                    "bad_fluency",
                    "grammar_agreement",
                    "missing_space",
                    "spanish_punctuation",
                    "token_or_placeholder_risk",
                    "proper_name_should_stay",
                    "insufficient_context",
                ],
            },
        },
    },
    "required": [
        "decision",
        "corrected_text",
        "confidence_score",
        "reason",
        "detected_issues",
    ],
}


def load_dotenv(path: Path) -> list[str]:
    loaded = []
    if not path.exists():
        return loaded
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def protected_tokens(value: str | None) -> Counter:
    if not value:
        return Counter()
    return Counter(normalize_protected_token(token) for token in PROTECTED_TOKEN_PATTERN.findall(value))


def normalize_protected_token(token: str) -> str:
    if not (token.startswith("[") and token.endswith("]")):
        return token

    command_name = token[1:].split("(", 1)[0].split("|", 1)[0].strip()
    base_name = command_name.split(".")[-1]

    if base_name == "Concept":
        seen = 0

        def replace_concept_literal(match: re.Match) -> str:
            nonlocal seen
            seen += 1
            if seen == 1:
                return match.group(0)
            return "'<TEXT>'"

        return STRING_LITERAL_PATTERN.sub(replace_concept_literal, token)

    if base_name in {
        "Select_CString",
        "SelectLocalization",
        "LocalPlayerString",
        "PlayerString",
        "GetString",
    } or base_name.startswith("SelectLocalization") or base_name.endswith("String"):
        return STRING_LITERAL_PATTERN.sub("'<TEXT>'", token)

    return token


def validate_tokens(spanish_text: str | None, reviewed_text: str | None) -> tuple[str, dict]:
    source_tokens = protected_tokens(spanish_text)
    reviewed_tokens = protected_tokens(reviewed_text)
    if source_tokens == reviewed_tokens:
        return "ok", {}
    missing = list((source_tokens - reviewed_tokens).elements())
    extra = list((reviewed_tokens - source_tokens).elements())
    return (
        "missing_tokens" if missing else "extra_tokens",
        {"missing": missing[:30], "extra": extra[:30]},
    )


def persistent_residue_hits(value: str | None) -> list[str]:
    if not value:
        return []
    normalized = value.casefold()
    hits = []
    for term in PERSISTENT_SPANISH_RESIDUES:
        if re.search(rf"\b{re.escape(term)}\b", normalized):
            hits.append(term)
    return sorted(hits)


def has_spanish_angular_quotes(value: str | None) -> bool:
    if not value:
        return False
    return any(mark in value for mark in SPANISH_ANGULAR_QUOTE_MARKS)


def calibrate_confidence(review: dict, final_text: str | None, token_status: str) -> tuple[float, list[str]]:
    confidence = max(0.0, min(1.0, float(review["confidence_score"])))
    caps = []
    issues = set(review.get("detected_issues", []))

    residue_hits = persistent_residue_hits(final_text)
    if residue_hits and review["decision"] in {"accepted", "edited", "accepted_old"}:
        confidence = min(confidence, 0.74)
        caps.append("persistent_spanish_residue")
        issues.add("spanish_residue")

    if has_spanish_angular_quotes(final_text) and review["decision"] in {"accepted", "edited", "accepted_old"}:
        confidence = min(confidence, 0.74)
        caps.append("spanish_angular_quotes")
        issues.add("spanish_punctuation")

    if token_status != "ok" and review["decision"] in {"accepted", "edited", "accepted_old"}:
        confidence = min(confidence, 0.30)
        caps.append("token_validation_failed")
        issues.add("token_or_placeholder_risk")

    if "insufficient_context" in issues:
        confidence = min(confidence, 0.84)
        caps.append("insufficient_context")

    review["confidence_score"] = confidence
    review["detected_issues"] = sorted(issues)
    return confidence, caps


def final_text_for_review(row, decision: str, corrected_text: str | None) -> str | None:
    if decision == "edited":
        return corrected_text
    if decision == "accepted_old":
        return row["old_text"]
    if decision == "accepted":
        return row["suggested_text"]
    return None


def load_pending_rows(conn, limit: int, prompt_version: str):
    return conn.execute(
        """
        WITH ranked_feedback AS (
            SELECT
                f.id AS feedback_id,
                f.suggestion_id,
                f.segment_id,
                f.suggested_text,
                s.relative_path,
                s.source_key,
                s.source_line_number,
                s.spanish_text,
                s.english_text,
                s.old_text,
                a.confidence_score AS segment_confidence_score,
                a.classification,
                ts.source_language,
                ts.origin,
                ts.match_type,
                ts.match_score,
                ts.token_status,
                ts.status AS suggestion_status,
                ts.reasons_json,
                ROW_NUMBER() OVER (
                    PARTITION BY f.segment_id
                    ORDER BY
                        CASE
                            WHEN ts.origin LIKE 'human_feedback_%' THEN 0
                            WHEN ts.status = 'safe' THEN 1
                            WHEN ts.status = 'review' THEN 2
                            ELSE 3
                        END,
                        CASE
                            WHEN ts.match_type = 'persistent_residue' THEN 0
                            WHEN ts.match_type = 'spanish_angular_quotes' THEN 1
                            WHEN ts.match_type = 'missing_space_after_token' THEN 2
                            WHEN ts.match_type = 'exact_spanish' THEN 3
                            WHEN ts.match_type = 'exact_english' THEN 4
                            WHEN ts.match_type = 'fuzzy_spanish' THEN 5
                            WHEN ts.match_type = 'fuzzy_english' THEN 6
                            ELSE 7
                        END,
                        ts.match_score DESC,
                        f.id ASC
                ) AS rank_for_segment
            FROM suggestion_feedback f
            JOIN source_segments s ON s.id = f.segment_id
            LEFT JOIN segment_analysis a ON a.segment_id = s.id
            LEFT JOIN translation_suggestions ts ON ts.id = f.suggestion_id
            WHERE f.decision = 'pending'
              AND NOT EXISTS (
                  SELECT 1
                  FROM suggestion_feedback resolved
                  WHERE resolved.segment_id = f.segment_id
                    AND resolved.decision IN ('accepted', 'edited', 'accepted_old')
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM api_reviews ar
                  WHERE ar.segment_id = f.segment_id
                    AND ar.prompt_version = ?
                    AND ar.status IN ('pending_human', 'approved', 'auto_ready', 'auto_applied')
              )
        )
        SELECT
            feedback_id,
            suggestion_id,
            segment_id,
            suggested_text,
            relative_path,
            source_key,
            source_line_number,
            spanish_text,
            english_text,
            old_text,
            segment_confidence_score,
            classification,
            source_language,
            origin,
            match_type,
            match_score,
            token_status,
            reasons_json
        FROM ranked_feedback
        WHERE rank_for_segment = 1
        ORDER BY
            CASE WHEN suggestion_status = 'safe' THEN 0 ELSE 1 END,
            match_score DESC,
            feedback_id ASC
        LIMIT ?
        """,
        (prompt_version, limit),
    ).fetchall()


def build_prompt(row) -> list[dict]:
    system = (
        "Voce e um revisor de localizacao pt-BR para Crusader Kings III. "
        "Avalie a sugestao preservando a estrutura de tokens, placeholders, tags, comandos CK3, "
        "variaveis entre colchetes, marcadores com $, #, @ ou \\n. "
        "Use o espanhol como espelho estrutural e o ingles como referencia semantica. "
        "Dentro de comandos CK3, preserve nomes de comando, pipes e variaveis, mas traduza argumentos textuais "
        "quando eles aparecem como texto humano. Em Concept('key', 'texto exibido'), preserve o primeiro argumento "
        "como chave reservada e traduza o segundo argumento exibido. "
        "Em LocalPlayerString/PlayerString/Select_CString, as opcoes entre aspas geralmente sao texto exibido e devem "
        "ser traduzidas quando estiverem em espanhol. O mesmo vale para SelectLocalization e variacoes como SelectLocalization_int32. "
        "Prefira accepted_old quando old_text estiver melhor que suggested_text. "
        "Use edited somente quando puder corrigir com alta seguranca preservando todos os tokens. "
        "Use rejected quando a sugestao estiver ruim e voce nao tiver seguranca para corrigir. "
        "Aspas angulares espanholas « e » nao devem aparecer na traducao pt-BR; remova esses caracteres, mantendo o texto. "
        "confidence_score deve medir seguranca de aplicar sem revisao humana: use >=0.95 somente quando nao houver "
        "residuo espanhol, erro de fluencia, risco de token ou duvida contextual; reduza a confianca se a correcao "
        "estiver apenas quase certa."
    )
    payload = {
        "segment_id": row["segment_id"],
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "spanish_text": row["spanish_text"] or "",
        "english_text": row["english_text"] or "",
        "old_text": row["old_text"] or "",
        "suggested_text": row["suggested_text"] or "",
        "suggestion_meta": {
            "source_language": row["source_language"],
            "origin": row["origin"],
            "match_type": row["match_type"],
            "match_score": row["match_score"],
            "token_status": row["token_status"],
            "reasons": json.loads(row["reasons_json"] or "[]"),
        },
    }
    user = (
        "Revise este segmento e responda apenas pelo schema. "
        "decision deve significar: accepted=suggested_text esta bom; "
        "accepted_old=old_text esta melhor/correto; edited=use corrected_text; "
        "rejected=sugestao ruim sem correcao segura. "
        "Remova aspas angulares espanholas « e » do texto final. "
        "Nao deixe residuos como decisiones, situación, situaciones, cortesanos, gobernantes, invitados, hacendado ou rechaza no texto final. "
        "Exemplo: [Concept('decision', 'decisiones')|E] deve virar [Concept('decision', 'decisões')|E], preservando a chave decision.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def get_output_text(response) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text
    if hasattr(response, "model_dump"):
        dumped = response.model_dump()
    else:
        dumped = response
    for item in dumped.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                return content["text"]
    raise RuntimeError("API response did not include output text.")


def call_api(client, model: str, row) -> dict:
    response = client.responses.create(
        model=model,
        input=build_prompt(row),
        text={
            "format": {
                "type": "json_schema",
                "name": "translation_review",
                "strict": True,
                "schema": REVIEW_SCHEMA,
            }
        },
    )
    parsed = json.loads(get_output_text(response))
    parsed["_raw_response"] = response.model_dump() if hasattr(response, "model_dump") else {}
    return parsed


def upsert_api_review(conn, row, model: str, prompt_version: str, review: dict) -> str:
    decision = review["decision"]
    corrected_text = review.get("corrected_text") or None
    final_text = final_text_for_review(row, decision, corrected_text)
    token_status = "not_applicable"
    token_details = {}
    if final_text is not None:
        token_status, token_details = validate_tokens(row["spanish_text"], final_text)
        _, confidence_caps = calibrate_confidence(review, final_text, token_status)
        if confidence_caps:
            token_details["confidence_caps"] = confidence_caps
        if token_status != "ok" and decision in {"accepted", "edited", "accepted_old"}:
            decision = "rejected"
            corrected_text = None
    else:
        review["confidence_score"] = max(0.0, min(1.0, float(review["confidence_score"])))

    now = db.utc_now()
    status = "pending_human"
    payload = (
        row["feedback_id"],
        row["suggestion_id"],
        row["segment_id"],
        model,
        prompt_version,
        decision,
        corrected_text,
        float(review["confidence_score"]),
        review.get("reason", ""),
        json.dumps(review.get("detected_issues", []), ensure_ascii=False),
        token_status,
        json.dumps(token_details, ensure_ascii=False),
        json.dumps(review, ensure_ascii=False),
        status,
        now,
        now,
    )
    existing = conn.execute(
        """
        SELECT id
        FROM api_reviews
        WHERE feedback_id = ?
          AND model = ?
          AND prompt_version = ?
        """,
        (row["feedback_id"], model, prompt_version),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE api_reviews
            SET
                suggestion_id = ?,
                segment_id = ?,
                decision_suggested = ?,
                corrected_text = ?,
                confidence_score = ?,
                reason = ?,
                detected_issues_json = ?,
                token_validation_status = ?,
                token_validation_details_json = ?,
                api_response_json = ?,
                status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                row["suggestion_id"],
                row["segment_id"],
                decision,
                corrected_text,
                float(review["confidence_score"]),
                review.get("reason", ""),
                json.dumps(review.get("detected_issues", []), ensure_ascii=False),
                token_status,
                json.dumps(token_details, ensure_ascii=False),
                json.dumps(review, ensure_ascii=False),
                status,
                now,
                existing["id"],
            ),
        )
        return "updated"

    conn.execute(
        """
        INSERT INTO api_reviews (
            feedback_id,
            suggestion_id,
            segment_id,
            model,
            prompt_version,
            decision_suggested,
            corrected_text,
            confidence_score,
            reason,
            detected_issues_json,
            token_validation_status,
            token_validation_details_json,
            api_response_json,
            status,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    return "inserted"


def main(
    limit: int | None = None,
    model_override: str | None = None,
    concurrency: int | None = None,
) -> None:
    if limit is None and model_override is None:
        parser = argparse.ArgumentParser(description="Review pending suggestions with the OpenAI API.")
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
        parser.add_argument("--model", default=None)
        parser.add_argument("--concurrency", type=int, default=None)
        args = parser.parse_args()
        limit = args.limit
        model_override = args.model
        concurrency = args.concurrency
    else:
        limit = DEFAULT_LIMIT if limit is None else limit

    settings = db.load_settings()
    loaded_env_keys = load_dotenv(db.PROJECT_ROOT / ".env")
    api_settings = settings.get("api_review", {})
    model = model_override or os.getenv("OPENAI_MODEL") or api_settings.get("model", "gpt-5-mini")
    prompt_version = api_settings.get("prompt_version", RULE_VERSION)
    concurrency = concurrency or int(api_settings.get("concurrency", 4))
    concurrency = max(1, concurrency)
    started_at = datetime.now()

    print("[validate_suggestions_api] Starting API review")
    print(f"[validate_suggestions_api] Rule version: {RULE_VERSION}")
    print(f"[validate_suggestions_api] Model: {model}")
    print(f"[validate_suggestions_api] Limit: {limit}")
    print(f"[validate_suggestions_api] Concurrency: {concurrency}")
    if loaded_env_keys:
        visible_keys = [key for key in loaded_env_keys if key != "OPENAI_API_KEY"]
        if "OPENAI_API_KEY" in loaded_env_keys:
            visible_keys.append("OPENAI_API_KEY")
        print(
            "[validate_suggestions_api] Loaded .env keys: "
            + ", ".join(visible_keys)
        )

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set.")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Install dependencies with: pip install -r requirements.txt") from exc

    client = OpenAI()
    inserted = 0
    updated = 0
    errors = 0
    decisions: Counter = Counter()

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = load_pending_rows(conn, limit, prompt_version)
        total = len(rows)
        print(f"[validate_suggestions_api] Pending rows to review: {total}")

        if rows:
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                future_to_row = {
                    executor.submit(call_api, client, model, row): row
                    for row in rows
                }
                for index, future in enumerate(as_completed(future_to_row), start=1):
                    row = future_to_row[future]
                    try:
                        review = future.result()
                        result = upsert_api_review(conn, row, model, prompt_version, review)
                        inserted += 1 if result == "inserted" else 0
                        updated += 1 if result == "updated" else 0
                        decisions[review["decision"]] += 1
                        conn.commit()
                    except Exception as exc:
                        errors += 1
                        print(
                            "[validate_suggestions_api] "
                            f"Error on feedback {row['feedback_id']}: {exc}"
                        )
                    if index % 5 == 0 or index == total:
                        print(f"[validate_suggestions_api] {index}/{total} reviewed")

    elapsed = datetime.now() - started_at
    report_lines = [
        "API suggestion review report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Model: {model}",
        f"Limit: {limit}",
        f"Concurrency: {concurrency}",
        "",
        "Summary:",
        f"- Reviews inserted: {inserted}",
        f"- Reviews updated: {updated}",
        f"- Errors: {errors}",
        "",
        "Decisions suggested:",
    ]
    for decision, count in sorted(decisions.items()):
        report_lines.append(f"- {decision}: {count}")
    report_path = db.write_report(settings, "validate_suggestions_api", report_lines)
    print(f"[validate_suggestions_api] Report: {report_path}")
    print("[validate_suggestions_api] Done")


if __name__ == "__main__":
    main()
