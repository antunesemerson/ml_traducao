from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens
from ml_train_risk import DEFAULT_FEATURE_SET, candidate_differs_from_output, language_features, make_text
from ml_train_risk import normalize_compare
from ml_train_risk import LANGUAGE_BLOCKING_FEATURES


RULE_VERSION = "ml_score_segments_v1"
HARD_STRUCTURE_ISSUES = {
    "mojibake_or_unexpected_script",
}
FIXABLE_ISSUES = {
    "spanish_punctuation",
    "missing_space_after_token",
    "missing_space_before_token",
    "gender_token_extra_suffix",
    "gender_token_joined_to_word",
    "spanish_residue_in_literal",
}
PRESERVED_PROPER_NAME_RELAXED_ISSUES = {
    "spanish_residue",
    "mojibake_or_unexpected_script",
}
KNOWN_UNSAFE_TITLE_TEXT_BY_KEY = {
    "h_india": {"India"},
    "k_aquitaine": {"Aquitania"},
    "k_sicily": {"Sicilia"},
    "d_tripolitania": {"Trípolitania"},
    "d_tripolitana_adj": {"tripolitanio"},
    "k_orthodox": {"Patriarcado ecuménico"},
    "d_finnish_band_adj": {"finlandés"},
    "c_mecklemburg_adj": {"mecklemburgués"},
    "c_tecklenburg_adj": {"tecklemburgués"},
    "c_hellas_adj": {"helénico"},
    "d_aragon_adj": {"aragonés"},
    "c_nandurbar_adj": {"nandurbarí"},
    "c_trencin_adj": {"trencíniano"},
    "c_saintois_adj": {"saintoisés"},
    "duke_theocracy_female": {"Archisacerdotisa"},
    "duke_theocracy_female_paganism_religion": {"Archisacerdotisa"},
}


def is_preserved_proper_name_or_title(row: dict[str, Any], language_flags: set[str]) -> bool:
    candidate = normalize_compare(row.get("candidate_text"))
    if not candidate:
        return False
    if candidate != normalize_compare(row.get("english_text")):
        return False
    if candidate != normalize_compare(row.get("spanish_text")):
        return False

    relative_path = row.get("relative_path") or ""
    source_key = row.get("source_key") or ""
    if "SAFE_SHARED_NAME_OR_MOTTO" in language_flags:
        return True
    if relative_path == "titles_l_spanish.yml" and source_key.startswith(("b_", "c_", "d_", "k_", "e_")):
        return True
    if relative_path == "dlc/tgp/dlc_tgp_other_titles_l_spanish.yml" and source_key.startswith(
        ("b_", "c_", "d_", "k_", "e_")
    ):
        return True
    if relative_path == "titles_l_spanish.yml" and (
        re.fullmatch(r"[A-Z0-9_]+", source_key) or source_key.startswith("title_")
    ):
        return True
    if relative_path == "titles_cultural_names_l_spanish.yml" and source_key.startswith("cn_"):
        return True
    if relative_path == "culture/culture_titles_l_spanish.yml":
        return True
    if relative_path.startswith("religion/"):
        if "_effect_" in source_key or source_key.endswith("_possessive"):
            return False
        return True
    return False


def is_exact_token_only_passthrough(row: dict[str, Any]) -> bool:
    candidate = normalize_compare(row.get("candidate_text"))
    if not candidate:
        return False
    if candidate != normalize_compare(row.get("english_text")):
        return False
    if candidate != normalize_compare(row.get("spanish_text")):
        return False
    text = (row.get("candidate_text") or "").strip()
    if not text:
        return False
    without_tokens = local_quality_validator.PROTECTED_TOKEN_PATTERN.sub(" ", text)
    without_tokens = re.sub(r"[$#@!|_:\[\].()0-9\\/-]+", " ", without_tokens)
    return not without_tokens.strip() and bool(protected_tokens(text))


def is_safe_dynasty_family_title(row: dict[str, Any]) -> bool:
    relative_path = row.get("relative_path") or ""
    source_key = row.get("source_key") or ""
    if relative_path == "titles_l_spanish.yml":
        allowed_prefixes = ("c_nf_", "d_nf_")
    elif relative_path == "dlc/tgp/dlc_tgp_other_titles_l_spanish.yml":
        allowed_prefixes = ("b_nf_", "c_nf_", "d_nf_", "k_nf_", "e_nf_")
    else:
        return False
    if not source_key.startswith(allowed_prefixes):
        return False

    candidate = row.get("candidate_text") or ""
    if normalize_compare(candidate) != normalize_compare(row.get("old_text")):
        return False
    if candidate_differs_from_output(row):
        return False

    english = row.get("english_text") or ""
    spanish = row.get("spanish_text") or ""
    return bool(
        re.fullmatch(r"Família \$dynn_[A-Za-z0-9_-]+\$", candidate.strip())
        and re.fullmatch(r"\$dynn_[A-Za-z0-9_-]+\$ Family", english.strip())
        and re.fullmatch(r"Familia \$dynn_[A-Za-z0-9_-]+\$", spanish.strip())
    )

def has_blocked_spanish_geography_terms(row: dict[str, Any]) -> bool:
    if (row.get("relative_path") or "") != "titles_l_spanish.yml":
        return False
    candidate = row.get("candidate_text") or ""
    return bool(
        re.search(
            r"\b(Caballer[oa]s?|Camino|Caminos|Pradera|Praderas|Bajo|Baja|Sur|dorad[oa]s?|Duero|Guardia)\b",
            candidate,
            flags=re.IGNORECASE,
        )
    )


def has_known_unsafe_title_text(row: dict[str, Any]) -> bool:
    if (row.get("relative_path") or "") != "titles_l_spanish.yml":
        return False
    source_key = row.get("source_key") or ""
    blocked_values = KNOWN_UNSAFE_TITLE_TEXT_BY_KEY.get(source_key)
    if not blocked_values:
        return False
    candidate = normalize_compare(row.get("candidate_text"))
    return any(candidate == normalize_compare(value) for value in blocked_values)


def has_separator_whitespace_loss(row: dict[str, Any]) -> bool:
    source_key = row.get("source_key") or ""
    if "SEPARATOR" not in source_key:
        return False
    candidate = row.get("candidate_text")
    reference = row.get("spanish_text") or row.get("old_text") or row.get("english_text")
    if candidate is None or reference is None:
        return False
    return str(candidate) != str(reference)


def is_safe_religion_old_adjective(row: dict[str, Any]) -> bool:
    relative_path = row.get("relative_path") or ""
    source_key = row.get("source_key") or ""
    candidate = (row.get("candidate_text") or "").strip()
    if not relative_path.startswith("religion/"):
        return False
    if not source_key.endswith("_old_adj"):
        return False
    return bool(re.fullmatch(r"\$[A-Za-z0-9_]+_adj\$ antiga", candidate))


def is_safe_contextual_taxonomy(
    row: dict[str, Any],
    safe_probability: float,
    safe_threshold: float,
) -> bool:
    if candidate_differs_from_output(row):
        return False
    if has_blocked_spanish_geography_terms(row):
        return False
    if has_known_unsafe_title_text(row):
        return False

    relative_path = row.get("relative_path") or ""
    source_key = row.get("source_key") or ""

    if relative_path == "titles_cultural_names_l_spanish.yml":
        if source_key.startswith("cn_") and not source_key.endswith("_adj") and safe_probability >= 0.795:
            return True

    if relative_path.startswith("religion/"):
        if (
            source_key.startswith("holy_site_")
            and source_key.endswith("_name")
            and "_effect_" not in source_key
            and safe_probability >= 0.79
        ):
            return True

    if safe_probability < 0.80:
        return False

    if relative_path == "titles_cultural_names_l_spanish.yml":
        return source_key.startswith("cn_")

    if relative_path == "titles_l_spanish.yml":
        title_threshold = max(0.80, safe_threshold)
        if (
            safe_probability >= title_threshold
            and source_key.startswith(("b_", "c_", "d_", "k_", "e_"))
            and (source_key.endswith("_adj") or source_key.startswith("b_"))
        ):
            return True

    if relative_path.startswith("religion/"):
        if is_safe_religion_old_adjective(row):
            return True
        if "_effect_" in source_key or source_key.endswith("_possessive"):
            return False
        return (
            (source_key.startswith("holy_site_") and source_key.endswith("_name"))
            or "_god_" in source_key
            or source_key.endswith("_god_name")
        )

    return False


def relax_contextual_validation(
    row: dict[str, Any],
    validation: dict[str, Any],
    language_flags: set[str],
) -> dict[str, Any]:
    if not is_preserved_proper_name_or_title(row, language_flags):
        return validation
    kept_issues = [
        issue for issue in validation["issues"] if issue["code"] not in PRESERVED_PROPER_NAME_RELAXED_ISSUES
    ]
    if len(kept_issues) == len(validation["issues"]):
        return validation
    relaxed = dict(validation)
    relaxed["issues"] = kept_issues
    relaxed["issue_count"] = len(kept_issues)
    relaxed["high_issue_count"] = sum(1 for issue in kept_issues if issue["severity"] == "high")
    relaxed["medium_issue_count"] = sum(1 for issue in kept_issues if issue["severity"] == "medium")
    relaxed["auto_approval_blocked"] = (
        relaxed["high_issue_count"] > 0 or relaxed["medium_issue_count"] > 0 or relaxed["word_count"] >= 70
    )
    relaxed["suppressed_issue_codes"] = sorted(
        {
            issue["code"]
            for issue in validation["issues"]
            if issue["code"] in PRESERVED_PROPER_NAME_RELAXED_ISSUES
        }
    )
    return relaxed


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percent(part: int, total: int) -> str:
    if total == 0:
        return "0.00%"
    return f"{part / total:.2%}"


def latest_model_run(conn) -> dict[str, Any]:
    active = conn.execute(
        """
        SELECT r.*
        FROM ml_model_registry registry
        JOIN ml_model_runs r ON r.id = registry.active_model_run_id
        WHERE registry.model_kind = 'risk_action_classifier'
        LIMIT 1
        """
    ).fetchone()
    if active is not None:
        return dict(active)

    row = conn.execute(
        """
        SELECT *
        FROM ml_model_runs
        WHERE model_kind = 'risk_action_classifier'
          AND model_path IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No trained risk model found. Run python pipeline/main.py ml-train-risk first.")
    return dict(row)


def model_run_by_id(conn, model_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_model_runs
        WHERE id = ?
          AND model_path IS NOT NULL
        """,
        (model_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No trained risk model found for run id {model_run_id}.")
    return dict(row)


def token_status(spanish_text: str | None, candidate_text: str | None) -> str:
    if protected_tokens(spanish_text) == protected_tokens(candidate_text):
        return "ok"
    return "mismatch"


def path_filter_sql(path_like: str | None) -> tuple[str, tuple[str, ...]]:
    if not path_like:
        return "", ()
    return "AND s.relative_path LIKE ?", (path_like,)


def qualify_trusted_scope_sql(scope_sql: str | None) -> str | None:
    if not scope_sql:
        return None
    qualified = re.sub(r"(?<![\w.])relative_path(?!\w)", "s.relative_path", scope_sql)
    qualified = re.sub(r"(?<![\w.])source_key(?!\w)", "s.source_key", qualified)
    return qualified


def fetch_segments(
    conn,
    limit: int | None,
    path_like: str | None,
    include_locked: bool,
    offset: int = 0,
    scope_sql: str | None = None,
) -> list[dict[str, Any]]:
    path_sql, path_params = path_filter_sql(path_like)
    qualified_scope_sql = qualify_trusted_scope_sql(scope_sql)
    scope_filter_sql = f"AND ({qualified_scope_sql})" if qualified_scope_sql else ""
    locked_sql = ""
    if not include_locked:
        locked_sql = """
          AND NOT (
              sc.confirmation_level IN ('human_confirmed', 'human')
              AND sc.locked = 1
          )
        """
    limit_sql = "LIMIT ? OFFSET ?" if limit else ""
    params: tuple[Any, ...] = (*path_params, limit, offset) if limit else path_params
    rows = conn.execute(
        f"""
        WITH token_counts AS (
            SELECT segment_id, COUNT(*) AS token_count
            FROM protected_tokens
            GROUP BY segment_id
        )
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            s.has_english,
            s.has_old,
            o.portuguese_text AS output_text,
            coalesce(o.portuguese_text, sc.confirmed_text, s.old_text, s.spanish_text, '') AS candidate_text,
            sc.confirmation_level,
            coalesce(sc.locked, 0) AS locked,
            sc.confidence_score AS confirmation_confidence,
            coalesce(tc.token_count, 0) AS token_count
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        LEFT JOIN token_counts tc ON tc.segment_id = s.id
        WHERE s.is_active = 1
          AND coalesce(o.portuguese_text, sc.confirmed_text, s.old_text, s.spanish_text, '') <> ''
          {path_sql}
          {scope_filter_sql}
          {locked_sql}
        ORDER BY
            CASE
                WHEN sc.confirmation_level = 'auto_confirmed' THEN 0
                WHEN sc.confirmation_level IS NULL THEN 1
                ELSE 2
            END,
            coalesce(sc.confidence_score, 0) ASC,
            s.id ASC
        {limit_sql}
        """,
        params,
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["text_length"] = len(data.get("candidate_text") or "")
        result.append(data)
    return result


def model_prediction(
    model,
    row: dict[str, Any],
    safe_threshold: float,
    feature_set: str,
) -> tuple[str, float, float, dict[str, float]]:
    text = make_text(row, feature_set=feature_set)
    classes = list(model.named_steps["classifier"].classes_)
    probabilities = model.predict_proba([text])[0]
    probability_by_class = {label: float(probabilities[index]) for index, label in enumerate(classes)}
    safe_probability = probability_by_class.get("auto_safe", 0.0)
    if safe_probability >= safe_threshold:
        return "auto_safe", safe_probability, safe_probability, probability_by_class
    non_safe = [(label, probability) for label, probability in probability_by_class.items() if label != "auto_safe"]
    action, confidence = max(non_safe, key=lambda item: item[1])
    return action, safe_probability, confidence, probability_by_class


def model_predictions(
    model,
    rows: list[dict[str, Any]],
    safe_threshold: float,
    feature_set: str,
) -> list[tuple[str, float, float, dict[str, float]]]:
    if not rows:
        return []
    texts = [make_text(row, feature_set=feature_set) for row in rows]
    classes = list(model.named_steps["classifier"].classes_)
    probabilities_by_row = model.predict_proba(texts)
    predictions: list[tuple[str, float, float, dict[str, float]]] = []
    for probabilities in probabilities_by_row:
        probability_by_class = {label: float(probabilities[index]) for index, label in enumerate(classes)}
        safe_probability = probability_by_class.get("auto_safe", 0.0)
        if safe_probability >= safe_threshold:
            predictions.append(("auto_safe", safe_probability, safe_probability, probability_by_class))
            continue
        non_safe = [(label, probability) for label, probability in probability_by_class.items() if label != "auto_safe"]
        action, confidence = max(non_safe, key=lambda item: item[1])
        predictions.append((action, safe_probability, confidence, probability_by_class))
    return predictions


def final_decision(
    row: dict[str, Any],
    model_action: str,
    safe_probability: float,
    model_confidence: float,
    probability_by_class: dict[str, float],
    safe_threshold: float,
) -> dict[str, Any]:
    candidate_text = row.get("candidate_text") or ""
    validation = local_quality_validator.validate_text(candidate_text)
    language_flags = set(language_features(row))
    validation = relax_contextual_validation(row, validation, language_flags)
    status = token_status(row.get("spanish_text"), candidate_text)
    issue_codes = {issue["code"] for issue in validation["issues"]}
    reasons = [
        f"model_action:{model_action}",
        f"safe_probability:{safe_probability:.4f}",
        f"safe_threshold:{safe_threshold:.4f}",
        f"model_confidence:{model_confidence:.4f}",
    ]
    deterministic_blocked = 0
    final_action = model_action
    risk_class = "low" if final_action == "auto_safe" else "medium"
    if model_action == "blocked_structure":
        final_action = "needs_human"
        risk_class = "high"
        reasons.append("model:structure_suspicion_needs_human")
    if validation.get("suppressed_issue_codes"):
        reasons.append(
            "deterministic:contextual_validation_exception:"
            + ",".join(validation["suppressed_issue_codes"])
        )

    if model_action == "auto_safe" and candidate_differs_from_output(row):
        final_action = "needs_human"
        risk_class = "high"
        deterministic_blocked = 1
        reasons.append("deterministic:candidate_differs_from_current_output")

    if final_action == "auto_safe" and has_blocked_spanish_geography_terms(row):
        final_action = "needs_human"
        risk_class = "high"
        deterministic_blocked = 1
        reasons.append("deterministic:block_spanish_geography_terms")

    if final_action == "auto_safe" and has_known_unsafe_title_text(row):
        final_action = "needs_human"
        risk_class = "high"
        deterministic_blocked = 1
        reasons.append("deterministic:block_known_unsafe_title_text")

    if final_action == "auto_safe" and has_separator_whitespace_loss(row):
        final_action = "needs_human"
        risk_class = "high"
        deterministic_blocked = 1
        reasons.append("deterministic:block_separator_whitespace_loss")

    if status != "ok":
        final_action = "blocked_structure"
        risk_class = "critical"
        deterministic_blocked = 1
        reasons.append("deterministic:block_token_mismatch")
    else:
        language_blocking_features = language_flags & LANGUAGE_BLOCKING_FEATURES
        if language_blocking_features == {"RISK_STRUCTURAL_EMPTY_VISIBLE_TEXT"} and is_exact_token_only_passthrough(row):
            language_blocking_features = set()
            reasons.append("deterministic:exact_token_only_passthrough")
        elif language_blocking_features:
            if final_action == "auto_safe":
                final_action = "needs_human"
                risk_class = "high"
                deterministic_blocked = 1
                reasons.append(
                    "deterministic:language_blocking_features:" + ",".join(sorted(language_blocking_features))
                )

    if deterministic_blocked:
        pass
    elif issue_codes & HARD_STRUCTURE_ISSUES:
        final_action = "blocked_structure"
        risk_class = "critical"
        deterministic_blocked = 1
        reasons.append("deterministic:hard_structure_issue")
    elif validation["auto_approval_blocked"]:
        if final_action == "auto_safe":
            final_action = "needs_human"
            risk_class = "high"
            deterministic_blocked = 1
            reasons.append("deterministic:auto_approval_blocked")
    elif issue_codes and issue_codes <= FIXABLE_ISSUES:
        if final_action == "auto_safe":
            final_action = "needs_autofix"
            risk_class = "medium"
            deterministic_blocked = 1
            reasons.append("deterministic:fixable_issues_override")
    elif validation["high_issue_count"] > 0:
        if final_action == "auto_safe":
            final_action = "needs_human"
            risk_class = "high"
            deterministic_blocked = 1
            reasons.append("deterministic:high_issue_override")

    if (
        not deterministic_blocked
        and final_action != "auto_safe"
        and status == "ok"
        and validation["issue_count"] == 0
        and is_exact_token_only_passthrough(row)
        and not candidate_differs_from_output(row)
    ):
        final_action = "auto_safe"
        risk_class = "low"
        reasons.append("deterministic:safe_exact_token_only_passthrough")

    if (
        not deterministic_blocked
        and final_action != "auto_safe"
        and status == "ok"
        and validation["issue_count"] == 0
        and is_preserved_proper_name_or_title(row, language_flags)
        and not candidate_differs_from_output(row)
    ):
        final_action = "auto_safe"
        risk_class = "low"
        reasons.append("deterministic:safe_preserved_proper_name_or_title")

    if (
        not deterministic_blocked
        and final_action != "auto_safe"
        and status == "ok"
        and validation["issue_count"] == 0
        and is_safe_contextual_taxonomy(row, safe_probability, safe_threshold)
    ):
        final_action = "auto_safe"
        risk_class = "low"
        reasons.append("deterministic:safe_contextual_taxonomy")

    if (
        not deterministic_blocked
        and final_action != "auto_safe"
        and status == "ok"
        and validation["issue_count"] == 0
        and is_safe_dynasty_family_title(row)
    ):
        final_action = "auto_safe"
        risk_class = "low"
        reasons.append("deterministic:safe_dynasty_family_title")

    if (
        not deterministic_blocked
        and final_action != "auto_safe"
        and status == "ok"
        and validation["issue_count"] == 0
        and is_safe_religion_old_adjective(row)
        and not candidate_differs_from_output(row)
    ):
        final_action = "auto_safe"
        risk_class = "low"
        reasons.append("deterministic:safe_religion_old_adjective")

    if final_action == "auto_safe":
        risk_class = "low"
    elif final_action == "blocked_structure":
        risk_class = "critical"
    elif final_action == "needs_autofix":
        risk_class = "medium"
    elif final_action == "needs_human":
        risk_class = "high" if validation["high_issue_count"] else "medium"

    reasons.append(
        "probabilities:"
        + ",".join(f"{key}={value:.4f}" for key, value in sorted(probability_by_class.items()))
    )
    language_reason_flags = sorted(
        flag
        for flag in language_flags
        if flag in LANGUAGE_BLOCKING_FEATURES
        or flag.startswith("SAFE_")
        or flag.startswith("VISIBLE_EQUALS_")
    )
    if language_reason_flags:
        reasons.append("language_features:" + ",".join(language_reason_flags))

    return {
        "segment_id": row["segment_id"],
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "candidate_text": candidate_text,
        "model_action": model_action,
        "final_action": final_action,
        "risk_class": risk_class,
        "model_safe_probability": round(safe_probability, 6),
        "model_confidence": round(model_confidence, 6),
        "token_status": status,
        "issue_count": validation["issue_count"],
        "high_issue_count": validation["high_issue_count"],
        "medium_issue_count": validation["medium_issue_count"],
        "word_count": validation["word_count"],
        "deterministic_blocked": deterministic_blocked,
        "confirmation_level": row.get("confirmation_level"),
        "locked": int(row.get("locked") or 0),
        "reasons": reasons,
        "issues": validation["issues"],
    }


def insert_run(
    conn,
    model_run: dict[str, Any],
    path_like: str | None,
    limit: int | None,
    started_at: str,
    scope_sql: str | None = None,
) -> int:
    notes = "Scores real segments with deterministic safety gates; does not apply changes."
    if scope_sql:
        notes += f" Trusted specialist scope: {scope_sql}"
    cursor = conn.execute(
        """
        INSERT INTO ml_score_runs (
            rule_version,
            model_run_id,
            model_version,
            path_filter,
            limit_count,
            notes,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            model_run["id"],
            model_run["model_version"],
            path_like or ("trusted_specialist_scope" if scope_sql else None),
            limit,
            notes,
            started_at,
            started_at,
        ),
    )
    return int(cursor.lastrowid)


def insert_items(conn, run_id: int, items: list[dict[str, Any]], created_at: str) -> None:
    conn.executemany(
        """
        INSERT OR IGNORE INTO ml_score_items (
            run_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            candidate_text,
            model_action,
            final_action,
            risk_class,
            model_safe_probability,
            model_confidence,
            token_status,
            issue_count,
            high_issue_count,
            medium_issue_count,
            word_count,
            deterministic_blocked,
            confirmation_level,
            locked,
            reasons_json,
            issues_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                item["segment_id"],
                item["relative_path"],
                item["source_key"],
                item["source_line_number"],
                item["candidate_text"],
                item["model_action"],
                item["final_action"],
                item["risk_class"],
                item["model_safe_probability"],
                item["model_confidence"],
                item["token_status"],
                item["issue_count"],
                item["high_issue_count"],
                item["medium_issue_count"],
                item["word_count"],
                item["deterministic_blocked"],
                item["confirmation_level"],
                item["locked"],
                json.dumps(item["reasons"], ensure_ascii=False),
                json.dumps(item["issues"], ensure_ascii=False),
                created_at,
            )
            for item in items
        ],
    )


def update_run_summary(conn, run_id: int, finished_at: str | None = None) -> Counter[str]:
    rows = conn.execute(
        """
        SELECT
            final_action,
            model_action,
            COUNT(*) AS total,
            SUM(deterministic_blocked) AS deterministic_blocks
        FROM ml_score_items
        WHERE run_id = ?
        GROUP BY final_action, model_action
        """,
        (run_id,),
    ).fetchall()
    final_counts: Counter[str] = Counter()
    model_counts: Counter[str] = Counter()
    deterministic_blocks = 0
    model_direct_auto_safe_count = 0
    deterministic_promoted_auto_safe_count = 0
    deterministic_demoted_auto_safe_count = 0
    for row in rows:
        total = int(row["total"] or 0)
        final_action = row["final_action"]
        model_action = row["model_action"]
        final_counts[final_action] += total
        model_counts[model_action] += total
        deterministic_blocks += int(row["deterministic_blocks"] or 0)
        if model_action == "auto_safe" and final_action == "auto_safe":
            model_direct_auto_safe_count += total
        elif model_action != "auto_safe" and final_action == "auto_safe":
            deterministic_promoted_auto_safe_count += total
        elif model_action == "auto_safe" and final_action != "auto_safe":
            deterministic_demoted_auto_safe_count += total
    scored_count = sum(final_counts.values())
    finished_sql = "finished_at = ?, updated_at = ?"
    timestamp_params: tuple[Any, ...] = (finished_at, finished_at) if finished_at else (now(),)
    if not finished_at:
        finished_sql = "updated_at = ?"
    conn.execute(
        f"""
        UPDATE ml_score_runs
        SET scored_count = ?,
            model_auto_safe_count = ?,
            model_direct_auto_safe_count = ?,
            deterministic_promoted_auto_safe_count = ?,
            deterministic_demoted_auto_safe_count = ?,
            final_auto_safe_count = ?,
            needs_human_count = ?,
            needs_autofix_count = ?,
            blocked_structure_count = ?,
            deterministic_block_count = ?,
            {finished_sql}
        WHERE id = ?
        """,
        (
            scored_count,
            model_counts["auto_safe"],
            model_direct_auto_safe_count,
            deterministic_promoted_auto_safe_count,
            deterministic_demoted_auto_safe_count,
            final_counts["auto_safe"],
            final_counts["needs_human"],
            final_counts["needs_autofix"],
            final_counts["blocked_structure"],
            deterministic_blocks,
            *timestamp_params,
            run_id,
        ),
    )
    return final_counts


def format_rows(rows) -> list[str]:
    if not rows:
        return ["- none: 0"]
    return [f"- {row['key'] or 'none'}: {row['total']}" for row in rows]


def main(
    limit: int | None = None,
    path_like: str | None = None,
    safe_threshold: float | None = None,
    include_locked: bool = False,
    batch_size: int = 5000,
    model_run_id: int | None = None,
    scope_sql: str | None = None,
) -> int:
    settings = db.load_settings()
    started_at_dt = datetime.now()
    started_at = started_at_dt.isoformat(timespec="seconds")
    print("[ml_score_segments] Starting ML segment scoring")
    print(f"[ml_score_segments] Rule version: {RULE_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        model_run = model_run_by_id(conn, model_run_id) if model_run_id is not None else latest_model_run(conn)
        threshold = safe_threshold if safe_threshold is not None else float(model_run["safe_threshold"] or 0.90)
        model_path = db.project_path(model_run["model_path"])
        bundle = joblib.load(model_path)
        model = bundle["model"]
        metadata = bundle.get("metadata", {})
        feature_set = metadata.get("feature_set") or DEFAULT_FEATURE_SET
        score_run_id = insert_run(conn, model_run, path_like, limit, started_at, scope_sql=scope_sql)
        conn.commit()
        print(f"[ml_score_segments] Model run id: {model_run['id']}")
        print(f"[ml_score_segments] Score run id: {score_run_id}")
        print(f"[ml_score_segments] Batch size: {batch_size}")
        if scope_sql:
            print("[ml_score_segments] Trusted specialist scope enabled")

        total_scored = 0
        offset = 0
        effective_batch_size = max(1, batch_size)
        while True:
            remaining = None if limit is None else max(limit - total_scored, 0)
            if remaining == 0:
                break
            current_limit = effective_batch_size if remaining is None else min(effective_batch_size, remaining)
            rows = fetch_segments(
                conn,
                limit=current_limit,
                path_like=path_like,
                include_locked=include_locked,
                offset=offset,
                scope_sql=scope_sql,
            )
            if not rows:
                break
            print(f"[ml_score_segments] Processing batch: offset={offset}, size={len(rows)}")
            items: list[dict[str, Any]] = []
            predictions = model_predictions(model, rows, threshold, feature_set)
            for row, prediction in zip(rows, predictions):
                model_action, safe_probability, model_confidence, probabilities = prediction
                items.append(
                    final_decision(
                        row,
                        model_action,
                        safe_probability,
                        model_confidence,
                        probabilities,
                        threshold,
                    )
                )
            insert_items(conn, score_run_id, items, started_at)
            total_scored += len(items)
            offset += len(rows)
            final_counts = update_run_summary(conn, score_run_id)
            conn.commit()
            print(
                "[ml_score_segments] Progress: "
                f"{total_scored} scored, final_auto_safe={final_counts['auto_safe']}, "
                f"needs_human={final_counts['needs_human']}, needs_autofix={final_counts['needs_autofix']}, "
                f"blocked_structure={final_counts['blocked_structure']}"
            )

        finished_at = now()
        final_counts = update_run_summary(conn, score_run_id, finished_at)
        conn.commit()

        model_action_rows = conn.execute(
            """
            SELECT model_action AS key, COUNT(*) AS total
            FROM ml_score_items
            WHERE run_id = ?
            GROUP BY model_action
            ORDER BY total DESC, key
            """,
            (score_run_id,),
        ).fetchall()
        final_action_rows = conn.execute(
            """
            SELECT final_action AS key, COUNT(*) AS total
            FROM ml_score_items
            WHERE run_id = ?
            GROUP BY final_action
            ORDER BY total DESC, key
            """,
            (score_run_id,),
        ).fetchall()
        risk_rows = conn.execute(
            """
            SELECT risk_class AS key, COUNT(*) AS total
            FROM ml_score_items
            WHERE run_id = ?
            GROUP BY risk_class
            ORDER BY total DESC, key
            """,
            (score_run_id,),
        ).fetchall()
        block_rows = conn.execute(
            """
            SELECT token_status AS key, COUNT(*) AS total
            FROM ml_score_items
            WHERE run_id = ?
            GROUP BY token_status
            ORDER BY total DESC, key
            """,
            (score_run_id,),
        ).fetchall()
        run_summary = conn.execute(
            """
            SELECT
                deterministic_block_count,
                model_auto_safe_count,
                model_direct_auto_safe_count,
                deterministic_promoted_auto_safe_count,
                deterministic_demoted_auto_safe_count
            FROM ml_score_runs
            WHERE id = ?
            """,
            (score_run_id,),
        ).fetchone()
        deterministic_blocks = int(run_summary["deterministic_block_count"] or 0) if run_summary else 0
        model_auto_safe_count = int(run_summary["model_auto_safe_count"] or 0) if run_summary else 0
        model_direct_auto_safe_count = int(run_summary["model_direct_auto_safe_count"] or 0) if run_summary else 0
        deterministic_promoted_auto_safe_count = (
            int(run_summary["deterministic_promoted_auto_safe_count"] or 0) if run_summary else 0
        )
        deterministic_demoted_auto_safe_count = (
            int(run_summary["deterministic_demoted_auto_safe_count"] or 0) if run_summary else 0
        )
        review_samples = conn.execute(
            """
            SELECT
                segment_id,
                relative_path,
                source_key,
                final_action,
                risk_class,
                model_safe_probability,
                token_status,
                issue_count
            FROM ml_score_items
            WHERE run_id = ?
              AND final_action <> 'auto_safe'
            ORDER BY
                CASE final_action
                    WHEN 'blocked_structure' THEN 0
                    WHEN 'needs_autofix' THEN 1
                    ELSE 2
                END,
                model_safe_probability DESC,
                segment_id
            LIMIT 20
            """,
            (score_run_id,),
        ).fetchall()

    elapsed = datetime.now() - started_at_dt
    total = sum(final_counts.values())
    report_lines = [
        "ML segment scoring report",
        f"Started at: {started_at}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Score run id: {score_run_id}",
        f"Model run id: {model_run['id']}",
        f"Model version: {model_run['model_version']}",
        f"Model path: {model_path}",
        f"Safe threshold: {threshold:.2f}",
        f"Feature set: {feature_set}",
        f"Path filter: {path_like or 'none'}",
        f"Trusted scope SQL: {scope_sql or 'none'}",
        f"Include locked: {include_locked}",
        "",
        "Summary:",
        f"- Scored segments: {total}",
        f"- Final auto safe: {final_counts['auto_safe']} ({percent(final_counts['auto_safe'], total)})",
        f"- Needs human: {final_counts['needs_human']} ({percent(final_counts['needs_human'], total)})",
        f"- Needs autofix: {final_counts['needs_autofix']} ({percent(final_counts['needs_autofix'], total)})",
        f"- Blocked structure: {final_counts['blocked_structure']} ({percent(final_counts['blocked_structure'], total)})",
        f"- Deterministic blocks/overrides: {deterministic_blocks}",
        "",
        "Auto-safe attribution:",
        f"- Model auto_safe before gates: {model_auto_safe_count} ({percent(model_auto_safe_count, total)})",
        f"- Model direct auto_safe after gates: {model_direct_auto_safe_count} ({percent(model_direct_auto_safe_count, total)})",
        f"- Deterministic promoted to auto_safe: {deterministic_promoted_auto_safe_count} ({percent(deterministic_promoted_auto_safe_count, total)})",
        f"- Model auto_safe demoted by gates: {deterministic_demoted_auto_safe_count} ({percent(deterministic_demoted_auto_safe_count, total)})",
        "",
        "Model action counts:",
        *format_rows(model_action_rows),
        "",
        "Final action counts after deterministic gates:",
        *format_rows(final_action_rows),
        "",
        "Risk classes:",
        *format_rows(risk_rows),
        "",
        "Token status:",
        *format_rows(block_rows),
        "",
        "Review samples:",
    ]
    if review_samples:
        report_lines.extend(
            [
                (
                    f"- segment {row['segment_id']} | {row['final_action']} | {row['risk_class']} | "
                    f"safe_prob={row['model_safe_probability']:.4f} | token={row['token_status']} | "
                    f"issues={row['issue_count']} | {row['relative_path']}::{row['source_key']}"
                )
                for row in review_samples
            ]
        )
    else:
        report_lines.append("- none")
    report_lines.extend(
        [
            "",
            "Interpretation:",
            "- This command only scores; it does not apply changes.",
            "- Deterministic gates can downgrade model auto_safe decisions.",
            "- Use this report to compare ML triage against existing heuristic queues.",
        ]
    )
    report_path = db.write_report(settings, "ml_score_segments", report_lines)

    print(f"[ml_score_segments] Scored segments: {total}")
    print(
        "[ml_score_segments] Final auto_safe: "
        f"{final_counts['auto_safe']}/{total} ({percent(final_counts['auto_safe'], total)})"
    )
    print(f"[ml_score_segments] Needs human: {final_counts['needs_human']}")
    print(f"[ml_score_segments] Needs autofix: {final_counts['needs_autofix']}")
    print(f"[ml_score_segments] Blocked structure: {final_counts['blocked_structure']}")
    print(f"[ml_score_segments] Deterministic blocks: {deterministic_blocks}")
    print(f"[ml_score_segments] Report: {report_path}")
    print("[ml_score_segments] Done")
    return score_run_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score real segments with the local ML risk model.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--path-like", default=None)
    parser.add_argument("--safe-threshold", type=float, default=None)
    parser.add_argument("--include-locked", action="store_true")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--model-run-id", type=int, default=None)
    args = parser.parse_args()
    main(
        limit=args.limit,
        path_like=args.path_like,
        safe_threshold=args.safe_threshold,
        include_locked=args.include_locked,
        batch_size=args.batch_size,
        model_run_id=args.model_run_id,
    )
