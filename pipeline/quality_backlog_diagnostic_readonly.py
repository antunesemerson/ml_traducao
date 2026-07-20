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

import db


RULE_VERSION = "quality_backlog_diagnostic_readonly_v1"
ES_HELPER_RE = re.compile(r"Custom\(\s*['\"]ES_[A-Za-z0-9_]+['\"]\s*\)", re.IGNORECASE)
SELECT_RE = re.compile(r"\bSelect_(?:CString|Localization)\s*\(", re.IGNORECASE)
TOKEN_RE = re.compile(r"\[[^\[\]\r\n]+\]|\$[^$\r\n]+\$|#[A-Za-z0-9_]+(?:\s+[^#!\r\n]+)?#!|\\n")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths() -> dict[str, Path]:
    reports = db.project_path(db.load_settings()["reports_dir"])
    reports.mkdir(parents=True, exist_ok=True)
    base = reports / f"{stamp()}_quality_backlog_diagnostic_readonly"
    return {
        "markdown": base.with_suffix(".md"),
        "jsonl": base.with_suffix(".jsonl"),
        "summary": base.with_name(base.name + "_summary.json"),
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


def parse_json_list(value: Any) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def normalized(value: str | None) -> str:
    return " ".join((value or "").strip().casefold().split())


def preview(value: str | None, limit: int = 360) -> str:
    text = (value or "").replace("\r", "").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def domain(relative_path: str) -> str:
    path = relative_path.replace("\\", "/")
    return path.split("/", 1)[0] if "/" in path else path


def issue_codes(issues: list[Any]) -> list[str]:
    return sorted(
        {
            str(item.get("code") or "unknown_issue")
            for item in issues
            if isinstance(item, dict)
        }
    )


def classify(row: dict[str, Any], codes: list[str]) -> tuple[str, str, bool]:
    output = str(row.get("output_text") or "")
    spanish = str(row.get("spanish_text") or "")
    english = str(row.get("english_text") or "")
    output_normalized = normalized(output)
    spanish_normalized = normalized(spanish)
    english_normalized = normalized(english)
    token_status = str(row.get("token_status") or "")
    final_action = str(row.get("final_action") or "")
    high_issues = int(row.get("high_issue_count") or 0)

    if token_status == "mismatch" or final_action == "blocked_structure":
        return "token_structure_authority", "architecture_token_guard", False
    if "spanish_residue_in_literal" in codes:
        return "spanish_literal_inside_dynamic", "shadow_spanish_literal_specialist", True
    if any("spanish_residue" in code for code in codes):
        return "spanish_residual_visible", "shadow_spanish_residual_repair", True
    if (
        output_normalized
        and output_normalized == spanish_normalized
        and output_normalized == english_normalized
    ):
        return "preserved_shared_literal", "score_calibration_shared_literal", False
    if ES_HELPER_RE.search(output):
        return "protected_spanish_helper", "score_calibration_protected_helper", False
    if output_normalized and output_normalized == spanish_normalized:
        return "untranslated_spanish", "shadow_translation_candidate", True
    if output_normalized and output_normalized == english_normalized:
        return "untranslated_english", "shadow_translation_candidate", True
    if SELECT_RE.search(output):
        return "dynamic_select_semantic", "parser_later_select_context", False
    if "\\n" in output or "\n" in output:
        return "multiline_semantic", "clustered_semantic_review", False
    if high_issues:
        return "high_issue_specialist", "specialist_issue_router", False
    if final_action == "needs_autofix":
        return "autofix_unclassified", "pattern_mining_autofix", False
    if final_action == "needs_human":
        return "semantic_human", "clustered_semantic_review", False
    if final_action == "auto_safe":
        return "deterministic_safe_low_probability", "score_calibration_audit", False
    return "unclassified_critical", "architecture_review", False


def collect_rows(conn: sqlite3.Connection, score_run_id: int, threshold: float) -> list[dict[str, Any]]:
    records = conn.execute(
        """
        SELECT
            score.segment_id,
            score.relative_path,
            score.source_key,
            score.source_line_number,
            score.model_safe_probability AS score,
            score.model_confidence,
            score.final_action,
            score.risk_class,
            score.token_status,
            score.issue_count,
            score.high_issue_count,
            score.medium_issue_count,
            score.reasons_json,
            score.issues_json,
            source.english_text,
            source.spanish_text,
            source.old_text,
            output.portuguese_text AS output_text
        FROM ml_score_items score
        JOIN source_segments source ON source.id = score.segment_id AND source.is_active = 1
        LEFT JOIN output_segments output ON output.segment_id = score.segment_id
        WHERE score.run_id = ?
          AND score.model_safe_probability IS NOT NULL
          AND score.model_safe_probability < ?
        ORDER BY score.model_safe_probability ASC, score.high_issue_count DESC, score.segment_id ASC
        """,
        (score_run_id, threshold),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for source_row in records:
        row = dict(source_row)
        issues = parse_json_list(row.pop("issues_json", None))
        reasons = [str(item) for item in parse_json_list(row.pop("reasons_json", None))]
        codes = issue_codes(issues)
        route, next_stage, shadow_eligible = classify(row, codes)
        output_text = str(row.pop("output_text") or "")
        english_text = str(row.pop("english_text") or "")
        spanish_text = str(row.pop("spanish_text") or "")
        old_text = str(row.pop("old_text") or "")
        output.append(
            {
                **row,
                "quality_band": "critical" if float(row["score"]) < 0.20 else "low",
                "diagnostic_route": route,
                "recommended_next_stage": next_stage,
                "shadow_candidate_eligible": shadow_eligible,
                "issue_codes": codes,
                "language_features": sorted(
                    reason.split("language_features:", 1)[1]
                    for reason in reasons
                    if reason.startswith("language_features:")
                ),
                "domain": domain(str(row.get("relative_path") or "")),
                "token_count": len(TOKEN_RE.findall(output_text)),
                "output_hash": hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
                "english_preview": preview(english_text),
                "spanish_preview": preview(spanish_text),
                "old_preview": preview(old_text),
                "output_preview": preview(output_text),
                "candidate_generation_allowed": False,
                "apply_allowed": False,
            }
        )
    return output


def counter_dict(counter: Counter[str]) -> dict[str, int]:
    return dict(counter.most_common())


def write_reports(
    paths: dict[str, Path],
    score_run: dict[str, Any],
    rows: list[dict[str, Any]],
    threshold: float,
) -> dict[str, Any]:
    route_counts = Counter(str(row["diagnostic_route"]) for row in rows)
    next_stage_counts = Counter(str(row["recommended_next_stage"]) for row in rows)
    action_counts = Counter(str(row.get("final_action") or "unknown") for row in rows)
    risk_counts = Counter(str(row.get("risk_class") or "unknown") for row in rows)
    token_counts = Counter(str(row.get("token_status") or "unknown") for row in rows)
    issue_counts: Counter[str] = Counter()
    domain_counts = Counter(str(row.get("domain") or "unknown") for row in rows)
    route_scores: dict[str, list[float]] = defaultdict(list)
    route_samples: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        route = str(row["diagnostic_route"])
        route_scores[route].append(float(row["score"]))
        if len(route_samples[route]) < 10:
            route_samples[route].append(int(row["segment_id"]))
        issue_counts.update(str(code) for code in row.get("issue_codes") or [])

    route_summary = [
        {
            "route": route,
            "count": count,
            "average_score": round(sum(route_scores[route]) / len(route_scores[route]), 6),
            "sample_segment_ids": route_samples[route],
        }
        for route, count in route_counts.most_common()
    ]
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "read_only": True,
        "score_run_id": int(score_run["id"]),
        "score_rule_version": score_run.get("rule_version"),
        "model_version": score_run.get("model_version"),
        "candidate_text_source": score_run.get("candidate_text_source"),
        "threshold": threshold,
        "record_count": len(rows),
        "shadow_candidate_eligible_count": sum(bool(row["shadow_candidate_eligible"]) for row in rows),
        "route_counts": counter_dict(route_counts),
        "next_stage_counts": counter_dict(next_stage_counts),
        "action_counts": counter_dict(action_counts),
        "risk_counts": counter_dict(risk_counts),
        "token_status_counts": counter_dict(token_counts),
        "issue_code_counts": counter_dict(issue_counts),
        "domain_counts": counter_dict(domain_counts),
        "route_summary": route_summary,
        "candidate_generation": 0,
        "apply": 0,
        "source_changed": False,
        "output_changed": False,
    }
    paths["summary"].write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with paths["jsonl"].open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "# Quality backlog diagnostic (read-only)",
        "",
        f"- Score run: `{summary['score_run_id']}`",
        f"- Score threshold: `< {threshold:.2f}`",
        f"- Records: `{len(rows)}`",
        f"- Shadow candidate route eligible: `{summary['shadow_candidate_eligible_count']}`",
        "- Candidate generation: `0`",
        "- Apply: `0`",
        "",
        "## Ranked routes",
        "",
    ]
    for route in route_summary:
        lines.append(
            f"- `{route['route']}`: `{route['count']}`; avg score `{route['average_score']:.4f}`; "
            f"samples `{', '.join(str(value) for value in route['sample_segment_ids'])}`"
        )
    for title, values in (
        ("Next stages", next_stage_counts),
        ("Actions", action_counts),
        ("Token status", token_counts),
        ("Issue codes", issue_counts),
        ("Top domains", domain_counts),
    ):
        lines.extend(["", f"## {title}", ""])
        lines.extend(f"- `{name}`: `{count}`" for name, count in values.most_common(30))
    paths["markdown"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Classify the low-score quality backlog without changing state.")
    parser.add_argument("--score-run-id", type=int)
    parser.add_argument("--threshold", type=float, default=0.20)
    args = parser.parse_args()
    if not 0 < args.threshold <= 0.50:
        raise ValueError("threshold must be greater than 0 and at most 0.50")

    settings = db.load_settings()
    database_path = db.project_path(settings["database_path"])
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=120) as conn:
        conn.row_factory = sqlite3.Row
        score_run = latest_full_output_score_run(conn, args.score_run_id)
        rows = collect_rows(conn, int(score_run["id"]), args.threshold)
    paths = report_paths()
    summary = write_reports(paths, score_run, rows, args.threshold)
    print("[quality-backlog] Read-only diagnostic completed")
    print(f"[quality-backlog] Score run: {summary['score_run_id']}")
    print(f"[quality-backlog] Records: {summary['record_count']}")
    print(f"[quality-backlog] Shadow eligible: {summary['shadow_candidate_eligible_count']}")
    print(f"[quality-backlog] Markdown: {paths['markdown']}")
    print(f"[quality-backlog] JSONL: {paths['jsonl']}")
    print(f"[quality-backlog] Summary: {paths['summary']}")
    return summary


if __name__ == "__main__":
    main()
