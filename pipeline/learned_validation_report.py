from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "learned_validation_report_v1"
MODEL_VERSION = "heuristic_bootstrap"

FIXABLE_ISSUES = {
    "spanish_punctuation",
    "missing_space_after_token",
    "missing_space_before_token",
    "gender_token_extra_suffix",
    "gender_token_joined_to_word",
    "spanish_residue_in_literal",
}

HARD_STRUCTURE_ISSUES = {
    "mojibake_or_unexpected_script",
}

SENSITIVE_PATH_PARTS = (
    "/events/",
    "event_localization/",
    "schemes/",
    "story_cycle",
    "travel_events/",
)
TECHNICAL_PATH_PREFIXES = (
    "custom_localization/",
    "debug",
)
TECHNICAL_KEY_PREFIXES = (
    "CustomLoc_ES_",
)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percent(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return part / total * 100


def token_status(spanish_text: str | None, candidate_text: str | None) -> str:
    if protected_tokens(spanish_text) == protected_tokens(candidate_text):
        return "ok"
    return "mismatch"


def path_filter_sql(path_like: str | None) -> tuple[str, tuple[str, ...]]:
    if not path_like:
        return "", ()
    return "AND s.relative_path LIKE ?", (path_like,)


def chunks(values: list[int], size: int = 800):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def fetch_best_suggestions(conn, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    suggestions: dict[int, dict[str, Any]] = {}
    for batch in chunks(segment_ids):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT *
            FROM (
                SELECT
                    ts.id AS suggestion_id,
                    ts.segment_id,
                    ts.suggested_text,
                    ts.status AS suggestion_status,
                    ts.match_score,
                    ts.token_status AS suggestion_token_status,
                    ROW_NUMBER() OVER (
                        PARTITION BY ts.segment_id
                        ORDER BY
                            CASE WHEN ts.status = 'safe' THEN 0 ELSE 1 END,
                            ts.match_score DESC,
                            ts.id DESC
                    ) AS rn
                FROM translation_suggestions ts
                WHERE ts.segment_id IN ({placeholders})
            ) ranked
            WHERE rn = 1
            """,
            tuple(batch),
        ).fetchall()
        for row in rows:
            suggestions[int(row["segment_id"])] = dict(row)
    return suggestions


def fetch_pending_segments(conn, limit: int | None, path_like: str | None) -> list[dict[str, Any]]:
    path_sql, path_params = path_filter_sql(path_like)
    limit_sql = "LIMIT ?" if limit else ""
    params: tuple[Any, ...] = (*path_params, limit) if limit else path_params
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
            s.has_old,
            a.classification,
            a.confidence_score AS analysis_confidence
        FROM source_segments s
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        LEFT JOIN segment_analysis a ON a.segment_id = s.id
        WHERE s.is_active = 1
          AND sc.segment_id IS NULL
          {path_sql}
        ORDER BY
            CASE WHEN a.classification = 'trusted' THEN 0 ELSE 1 END,
            length(coalesce(s.old_text, s.spanish_text, '')) ASC,
            s.id ASC
        {limit_sql}
        """,
        params,
    ).fetchall()
    result = [dict(row) for row in rows]
    suggestions = fetch_best_suggestions(conn, [int(row["segment_id"]) for row in result])
    for row in result:
        suggestion = suggestions.get(int(row["segment_id"]), {})
        row["suggestion_id"] = suggestion.get("suggestion_id")
        row["suggested_text"] = suggestion.get("suggested_text")
        row["suggestion_status"] = suggestion.get("suggestion_status")
        row["match_score"] = suggestion.get("match_score")
        row["suggestion_token_status"] = suggestion.get("suggestion_token_status")
    return result


def choose_candidate(row: dict[str, Any]) -> tuple[str, str]:
    suggested = row.get("suggested_text")
    if suggested and row.get("suggestion_status") == "safe":
        return "safe_suggestion", str(suggested)
    old = row.get("old_text")
    if old:
        return "old_text", str(old)
    spanish = row.get("spanish_text")
    if spanish:
        return "spanish_source", str(spanish)
    return "empty", ""


def is_sensitive_path(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/")
    return any(part in normalized for part in SENSITIVE_PATH_PARTS)


def is_technical_row(row: dict[str, Any]) -> bool:
    relative_path = str(row.get("relative_path") or "")
    source_key = str(row.get("source_key") or "")
    return relative_path.startswith(TECHNICAL_PATH_PREFIXES) or source_key.startswith(TECHNICAL_KEY_PREFIXES)


def score_candidate(row: dict[str, Any], candidate_source: str, candidate_text: str) -> tuple[float, list[str]]:
    reasons: list[str] = [f"candidate:{candidate_source}"]
    score = 0.35

    classification = row.get("classification")
    analysis_confidence = float(row.get("analysis_confidence") or 0)
    if candidate_source == "old_text":
        if classification == "trusted":
            score = 0.88
            reasons.append("analysis:trusted")
        elif classification == "review_light":
            score = 0.68
            reasons.append("analysis:review_light")
        else:
            score = 0.52
            reasons.append(f"analysis:{classification or 'missing'}")
        if analysis_confidence >= 0.99:
            score += 0.06
            reasons.append("analysis_confidence:0.99+")
        elif analysis_confidence >= 0.90:
            score += 0.03
            reasons.append("analysis_confidence:0.90+")
    elif candidate_source == "safe_suggestion":
        score = 0.72
        match_score = float(row.get("match_score") or 0)
        if row.get("suggestion_token_status") == "ok":
            score += 0.08
            reasons.append("suggestion_tokens:ok")
        if match_score >= 0.98:
            score += 0.08
            reasons.append("match_score:0.98+")
        elif match_score >= 0.92:
            score += 0.04
            reasons.append("match_score:0.92+")
    elif candidate_source == "spanish_source":
        score = 0.20
        reasons.append("fallback:spanish_source")

    validation = local_quality_validator.validate_text(candidate_text)
    if validation["issue_count"] == 0:
        score += 0.07
        reasons.append("validator:clean")
    else:
        score -= min(0.35, 0.12 * validation["high_issue_count"] + 0.06 * validation["medium_issue_count"])
        reasons.append("validator:issues")

    words = int(validation["word_count"])
    if words <= 8:
        score += 0.04
        reasons.append("length:short")
    elif words >= 70:
        score -= 0.12
        reasons.append("length:long")
    elif words >= 30:
        score -= 0.04
        reasons.append("length:medium_long")

    status = token_status(row.get("spanish_text"), candidate_text)
    if status == "ok":
        score += 0.06
        reasons.append("protected_tokens:ok")
    else:
        score -= 0.50
        reasons.append("protected_tokens:mismatch")

    if is_sensitive_path(str(row.get("relative_path") or "")) and words >= 30:
        score -= 0.04
        reasons.append("sensitive_path:long_text")

    if validation["auto_approval_blocked"]:
        score = min(score, 0.74)
        reasons.append("validator:auto_blocked")

    if is_technical_row(row):
        score = min(score, 0.92)
        reasons.append("technical_row:audit_only")

    return max(0.0, min(score, 0.99)), reasons


def classify_item(row: dict[str, Any]) -> dict[str, Any]:
    candidate_source, candidate_text = choose_candidate(row)
    validation = local_quality_validator.validate_text(candidate_text)
    status = token_status(row.get("spanish_text"), candidate_text)
    score, reasons = score_candidate(row, candidate_source, candidate_text)
    issue_codes = {issue["code"] for issue in validation["issues"]}

    if status != "ok":
        action = "blocked_structure"
        risk_class = "critical"
        score = min(score, 0.30)
        reasons.append("action:block_token_mismatch")
    elif candidate_source == "empty":
        action = "needs_suggestion"
        risk_class = "high"
        reasons.append("action:no_candidate")
    elif issue_codes & HARD_STRUCTURE_ISSUES:
        action = "blocked_structure"
        risk_class = "critical"
        score = min(score, 0.45)
        reasons.append("action:hard_structure_issue")
    elif issue_codes and issue_codes <= FIXABLE_ISSUES:
        action = "needs_autofix"
        risk_class = "medium"
        score = min(score, 0.74)
        reasons.append("action:fixable_issues")
    elif any(issue["code"] == "spanish_residue" for issue in validation["issues"]):
        action = "needs_suggestion"
        risk_class = "high" if validation["high_issue_count"] else "medium"
        score = min(score, 0.68)
        reasons.append("action:spanish_residue")
    elif score >= 0.95 and not is_sensitive_path(str(row.get("relative_path") or "")) and not is_technical_row(row):
        action = "auto_safe"
        risk_class = "low"
        reasons.append("action:auto_safe")
    elif score >= 0.92 and validation["word_count"] <= 30:
        action = "auto_safe_audit"
        risk_class = "low"
        reasons.append("action:auto_safe_audit")
    elif score >= 0.86 and validation["issue_count"] == 0:
        action = "auto_safe_audit"
        risk_class = "medium"
        reasons.append("action:auto_safe_audit_medium")
    elif candidate_source in {"spanish_source", "empty"}:
        action = "needs_suggestion"
        risk_class = "high"
        reasons.append("action:needs_translation")
    else:
        action = "needs_human"
        risk_class = "medium"
        reasons.append("action:needs_human")

    return {
        "segment_id": row["segment_id"],
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "candidate_source": candidate_source,
        "candidate_text": candidate_text,
        "action": action,
        "risk_class": risk_class,
        "confidence_score": round(score, 4),
        "issue_count": validation["issue_count"],
        "high_issue_count": validation["high_issue_count"],
        "medium_issue_count": validation["medium_issue_count"],
        "word_count": validation["word_count"],
        "token_status": status,
        "reasons": reasons,
        "issues": validation["issues"],
    }


def insert_run(conn, path_like: str | None, limit: int | None, started_at: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO learned_validation_runs (
            rule_version, model_version, path_filter, limit_count, started_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (RULE_VERSION, MODEL_VERSION, path_like, limit, started_at, started_at),
    )
    return int(cursor.lastrowid)


def insert_items(conn, run_id: int, items: list[dict[str, Any]], created_at: str) -> None:
    conn.executemany(
        """
        INSERT INTO learned_validation_items (
            run_id, segment_id, relative_path, source_key, source_line_number,
            candidate_source, candidate_text, action, risk_class, confidence_score,
            issue_count, high_issue_count, medium_issue_count, word_count, token_status,
            reasons_json, issues_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                item["segment_id"],
                item["relative_path"],
                item["source_key"],
                item["source_line_number"],
                item["candidate_source"],
                item["candidate_text"],
                item["action"],
                item["risk_class"],
                item["confidence_score"],
                item["issue_count"],
                item["high_issue_count"],
                item["medium_issue_count"],
                item["word_count"],
                item["token_status"],
                json.dumps(item["reasons"], ensure_ascii=False),
                json.dumps(item["issues"], ensure_ascii=False),
                created_at,
            )
            for item in items
        ],
    )


def update_run_summary(
    conn,
    run_id: int,
    active_segments: int,
    pending_segments: int,
    action_counts: Counter[str],
    finished_at: str,
) -> None:
    conn.execute(
        """
        UPDATE learned_validation_runs
        SET active_segments = ?,
            pending_segments = ?,
            auto_safe_count = ?,
            auto_safe_audit_count = ?,
            needs_autofix_count = ?,
            needs_suggestion_count = ?,
            needs_human_count = ?,
            blocked_structure_count = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            active_segments,
            pending_segments,
            action_counts.get("auto_safe", 0),
            action_counts.get("auto_safe_audit", 0),
            action_counts.get("needs_autofix", 0),
            action_counts.get("needs_suggestion", 0),
            action_counts.get("needs_human", 0),
            action_counts.get("blocked_structure", 0),
            finished_at,
            finished_at,
            run_id,
        ),
    )


def build_report_lines(
    run_id: int,
    started_at: datetime,
    elapsed,
    active_segments: int,
    pending_segments: int,
    items: list[dict[str, Any]],
    path_like: str | None,
    limit: int | None,
) -> list[str]:
    action_counts = Counter(item["action"] for item in items)
    risk_counts = Counter(item["risk_class"] for item in items)
    source_counts = Counter(item["candidate_source"] for item in items)
    issue_counts: Counter[str] = Counter()
    package_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for item in items:
        package_counts[item["relative_path"]][item["action"]] += 1
        for issue in item["issues"]:
            issue_counts[issue["code"]] += 1

    lines = [
        "Learned validation report",
        f"Run id: {run_id}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Model version: {MODEL_VERSION}",
        f"Path filter: {path_like or 'none'}",
        f"Limit: {limit or 'none'}",
        "",
        "Coverage:",
        f"- Active segments: {active_segments}",
        f"- Pending unconfirmed segments scanned: {pending_segments}",
        f"- Items classified: {len(items)}",
        "",
        "Actions:",
        *[
            f"- {action}: {count} ({percent(count, len(items)):.2f}%)"
            for action, count in action_counts.most_common()
        ],
        "",
        "Risk classes:",
        *[
            f"- {risk}: {count} ({percent(count, len(items)):.2f}%)"
            for risk, count in risk_counts.most_common()
        ],
        "",
        "Candidate sources:",
        *[f"- {source}: {count}" for source, count in source_counts.most_common()],
        "",
        "Top issue codes:",
        *[f"- {code}: {count}" for code, count in issue_counts.most_common(20)],
        "",
        "Top packages by pending/risk:",
    ]

    def package_sort(item: tuple[str, Counter[str]]) -> tuple[int, int, str]:
        path, counts = item
        risky = counts.get("needs_human", 0) + counts.get("blocked_structure", 0) + counts.get("needs_suggestion", 0)
        return (-risky, -sum(counts.values()), path)

    for path, counts in sorted(package_counts.items(), key=package_sort)[:30]:
        total = sum(counts.values())
        details = ", ".join(f"{action}:{count}" for action, count in counts.most_common())
        lines.append(f"- {path}: {total} ({details})")

    lines.extend(["", "Samples by action:"])
    for action in [
        "blocked_structure",
        "needs_human",
        "needs_suggestion",
        "needs_autofix",
        "auto_safe_audit",
        "auto_safe",
    ]:
        samples = [item for item in items if item["action"] == action][:10]
        if not samples:
            continue
        lines.append(f"{action}:")
        for item in samples:
            reason = "; ".join(item["reasons"][:8])
            lines.append(
                f"- segment {item['segment_id']} | {item['confidence_score']:.3f} | "
                f"{item['relative_path']}::{item['source_key']} | {reason}"
            )
    return lines


def main(limit: int | None = None, path_like: str | None = None) -> None:
    settings = db.load_settings()
    started_at_dt = datetime.now()
    started_at = now()
    print("[learned_validation_report] Starting learned validation report")
    print(f"[learned_validation_report] Rule version: {RULE_VERSION}")
    print(f"[learned_validation_report] Model version: {MODEL_VERSION}")
    print(f"[learned_validation_report] Limit: {limit or 'none'}")
    print(f"[learned_validation_report] Path filter: {path_like or 'none'}")
    print(f"[learned_validation_report] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        active_segments = int(
            conn.execute("SELECT COUNT(*) FROM source_segments WHERE is_active = 1").fetchone()[0] or 0
        )
        pending_total = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM source_segments s
                LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
                WHERE s.is_active = 1
                  AND sc.segment_id IS NULL
                """
            ).fetchone()[0]
            or 0
        )
        rows = fetch_pending_segments(conn, limit, path_like)
        items = [classify_item(row) for row in rows]
        run_id = insert_run(conn, path_like, limit, started_at)
        insert_items(conn, run_id, items, started_at)
        action_counts = Counter(item["action"] for item in items)
        finished_at = now()
        update_run_summary(conn, run_id, active_segments, pending_total, action_counts, finished_at)
        conn.commit()

    elapsed = datetime.now() - started_at_dt
    report_lines = build_report_lines(
        run_id=run_id,
        started_at=started_at_dt,
        elapsed=elapsed,
        active_segments=active_segments,
        pending_segments=pending_total,
        items=items,
        path_like=path_like,
        limit=limit,
    )
    report_path = db.write_report(settings, "learned_validation_report", report_lines)
    print(f"[learned_validation_report] Items classified: {len(items)}")
    for action, count in Counter(item["action"] for item in items).most_common():
        print(f"[learned_validation_report] {action}: {count}")
    print(f"[learned_validation_report] Report: {report_path}")
    print("[learned_validation_report] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Classify pending segments by learned/local validation risk.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum pending segments to classify.")
    parser.add_argument("--path-like", default=None, help="Optional SQL LIKE filter for source relative_path.")
    args = parser.parse_args()
    main(limit=args.limit, path_like=args.path_like)
