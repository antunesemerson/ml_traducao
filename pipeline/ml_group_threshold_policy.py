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
from ml_train_risk import LANGUAGE_BLOCKING_FEATURES, key_shape, language_features, path_group


RULE_VERSION = "ml_group_threshold_policy_v2"
DEFAULT_THRESHOLD = 0.86


FAMILY_POLICY_RULES: list[dict[str, Any]] = [
    {
        "name": "title_directional_north",
        "threshold": 0.45,
        "require_learned_positive": True,
        "description": "Title/toponym names that explicitly map North to Norte.",
    },
    {
        "name": "cultural_title_reviewed",
        "threshold": 0.60,
        "require_learned_positive": True,
        "description": "Cultural title names/adjectives only after human positive review.",
    },
    {
        "name": "culture_title_reviewed",
        "threshold": 0.60,
        "require_learned_positive": True,
        "description": "Culture title labels only after human positive review.",
    },
    {
        "name": "religion_possessive_lowercase",
        "threshold": 0.35,
        "require_learned_positive": True,
        "description": "Religion possessive keys with Portuguese lowercase 'de ...'.",
    },
    {
        "name": "religion_old_name_reviewed",
        "threshold": 0.35,
        "require_learned_positive": True,
        "description": "Religion names/adherents only after human positive review.",
    },
]


POLICY_RULES: list[dict[str, Any]] = [
    {"name": "names", "path_group": "names", "threshold": 0.84, "require_learned_positive": True},
    {"name": "dynasties", "path_group": "dynasties", "threshold": 0.84, "require_learned_positive": True},
    {"name": "title_names", "path_group": "titles", "key_shape": "title_name", "threshold": 0.86, "require_learned_positive": True},
    {"name": "title_adjectives", "path_group": "titles", "key_shape": "title_adjective", "threshold": 0.84, "require_learned_positive": True},
    {"name": "cultural_title_names", "relative_path": "titles_cultural_names_l_spanish.yml", "threshold": 0.86, "require_learned_positive": True},
    {"name": "mottos", "path_group": "mottos", "threshold": 0.92, "require_learned_positive": True},
    {"name": "core_uppercase_ui", "path_group": "core", "key_shape": "uppercase_ui", "threshold": 0.90, "require_learned_positive": True},
    {"name": "core_other", "path_group": "core", "threshold": 0.88, "require_learned_positive": True},
    {"name": "game_concepts", "path_group": "game_concepts", "key_shape": "game_concept", "threshold": 0.88, "require_learned_positive": True},
    {"name": "game_concepts_ui", "path_group": "game_concepts", "threshold": 0.90, "require_learned_positive": True},
    {"name": "culture", "path_group": "culture", "threshold": 0.88, "require_learned_positive": True},
    {
        "name": "religion_reviewed_positive",
        "path_group": "religion",
        "threshold": 0.84,
        "require_learned_positive": True,
    },
    {"name": "custom_localization", "path_group": "custom_localization", "threshold": 0.92, "require_learned_positive": True},
    {"name": "council_tasks_ui", "path_group": "council_tasks", "key_shape": "uppercase_ui", "threshold": 0.90, "require_learned_positive": True},
    {"name": "council_tasks", "path_group": "council_tasks", "threshold": 0.88, "require_learned_positive": True},
    {"name": "debug", "path_group": "other", "relative_path": "debug_l_spanish.yml", "threshold": 0.90, "require_learned_positive": True},
    {"name": "wars_ui", "path_group": "wars", "key_shape": "uppercase_ui", "threshold": 0.90, "require_learned_positive": True},
    {"name": "wars", "path_group": "wars", "threshold": 0.88, "require_learned_positive": True},
]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percent(part: int, total: int) -> str:
    if total == 0:
        return "0.00%"
    return f"{part / total:.2%}"


def latest_score_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_score_runs
        WHERE finished_at IS NOT NULL
          AND path_filter IS NULL
          AND limit_count IS NULL
          AND candidate_text_source = 'output'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT id
            FROM ml_score_runs
            WHERE finished_at IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        raise RuntimeError("No completed ml_score_runs found. Run ml-score first.")
    return int(row["id"])


def load_score_run(conn, score_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_score_runs
        WHERE id = ?
        """,
        (score_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No ml_score_runs row found for id {score_run_id}.")
    return dict(row)


def family_rule_for(row: dict[str, Any]) -> dict[str, Any] | None:
    relative_path = row.get("relative_path") or ""
    source_key = row.get("source_key") or ""
    candidate = row.get("candidate_text") or ""
    english = row.get("english_text") or ""

    if relative_path == "titles_l_spanish.yml":
        has_north_key = "north" in source_key.lower()
        has_north_source = bool(re.search(r"\bNorth\b", english))
        has_norte_candidate = bool(re.search(r"\bNorte\b", candidate))
        if has_norte_candidate and (has_north_key or has_north_source):
            return FAMILY_POLICY_RULES[0]

    if relative_path == "titles_cultural_names_l_spanish.yml":
        return FAMILY_POLICY_RULES[1]

    if relative_path == "culture/culture_titles_l_spanish.yml":
        return FAMILY_POLICY_RULES[2]

    if relative_path.startswith("religion/"):
        if source_key.endswith("_possessive") and re.fullmatch(r"de\s+\S(?:.*\S)?", candidate):
            return FAMILY_POLICY_RULES[3]
        if (
            source_key.endswith(("_adherent", "_adherent_plural"))
            or source_key.endswith("_old")
            or source_key.endswith("_name")
        ):
            return FAMILY_POLICY_RULES[4]

    return None


def policy_for(row: dict[str, Any]) -> tuple[str, float, dict[str, Any]]:
    family_rule = family_rule_for(row)
    if family_rule is not None:
        return str(family_rule["name"]), float(family_rule["threshold"]), family_rule
    row_path_group = path_group(row.get("relative_path"))
    row_key_shape = key_shape(row.get("source_key"))
    for rule in POLICY_RULES:
        if "relative_path" in rule and row.get("relative_path") != rule["relative_path"]:
            continue
        if "path_group" in rule and row_path_group != rule["path_group"]:
            continue
        if "key_shape" in rule and row_key_shape != rule["key_shape"]:
            continue
        return str(rule["name"]), float(rule["threshold"]), rule
    return "default", DEFAULT_THRESHOLD, {"name": "default", "threshold": DEFAULT_THRESHOLD, "require_learned_positive": True}


def candidate_equals_output(row: dict[str, Any]) -> bool:
    output_text = row.get("output_text")
    if output_text is None:
        return False
    return (row.get("candidate_text") or "") == (output_text or "")


def policy_allows_safe(
    row: dict[str, Any],
    threshold: float,
    rule: dict[str, Any] | None = None,
    enforce_language_blockers: bool = True,
) -> bool:
    if int(row.get("learned_negative") or 0):
        return False
    if rule and rule.get("require_learned_positive") and not int(row.get("learned_positive") or 0):
        return False
    if enforce_language_blockers and set(language_features(row)) & LANGUAGE_BLOCKING_FEATURES:
        return False
    if float(row.get("model_safe_probability") or 0.0) < threshold:
        return False
    if int(row.get("deterministic_blocked") or 0):
        return False
    if row.get("token_status") != "ok":
        return False
    if int(row.get("issue_count") or 0):
        return False
    if int(row.get("high_issue_count") or 0):
        return False
    if int(row.get("medium_issue_count") or 0):
        return False
    if not candidate_equals_output(row):
        return False
    return True


def fetch_items(conn, score_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            i.*,
            i.id AS score_item_id,
            s.english_text,
            s.spanish_text,
            o.portuguese_text AS output_text,
            EXISTS (
                SELECT 1
                FROM local_learning_candidates l
                WHERE l.segment_id = i.segment_id
                  AND l.local_status = 'reviewed_human'
                  AND l.human_label IN (
                    'major_fix',
                    'minor_fix',
                    'rejected',
                    'rejected_suggestion',
                    'residual_spanish',
                    'semantic_error',
                    'structure_error',
                    'token_mismatch'
                  )
            ) AS learned_negative,
            EXISTS (
                SELECT 1
                FROM local_learning_candidates l
                WHERE l.segment_id = i.segment_id
                  AND l.local_status = 'reviewed_human'
                  AND l.human_label IN ('contextual_exception', 'correct')
            ) AS learned_positive
        FROM ml_score_items i
        JOIN source_segments s ON s.id = i.segment_id
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        WHERE i.run_id = ?
        ORDER BY i.segment_id
        """,
        (score_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def policy_reasons(
    row: dict[str, Any],
    rule: dict[str, Any],
    active_safe: bool,
    raw_policy_safe: bool,
    policy_safe: bool,
    protect_active_safe: bool,
) -> list[str]:
    reasons = [
        f"policy_group:{row['policy_group']}",
        f"policy_threshold:{float(row['policy_threshold']):.2f}",
        f"safe_probability:{float(row.get('model_safe_probability') or 0.0):.4f}",
        f"score_final_action:{row.get('final_action')}",
    ]
    if rule.get("require_learned_positive"):
        reasons.append("policy_requires:learned_positive")
    if row.get("learned_positive"):
        reasons.append("learned:positive")
    if row.get("learned_negative"):
        reasons.append("learned:negative")
    if active_safe and protect_active_safe:
        reasons.append("policy:protect_active_safe")
    if raw_policy_safe:
        reasons.append("policy:raw_safe")
    if policy_safe and not active_safe:
        reasons.append("policy:new_safe")
    blockers = sorted(set(language_features(row)) & LANGUAGE_BLOCKING_FEATURES)
    if blockers:
        reasons.append("language_blockers:" + ",".join(blockers))
    return reasons


def insert_policy_run(
    conn,
    score_run: dict[str, Any],
    started_at: datetime,
    protect_active_safe: bool,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO ml_policy_runs (
            rule_version,
            score_run_id,
            model_run_id,
            model_version,
            protect_active_safe,
            notes,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            score_run["id"],
            score_run["model_run_id"],
            score_run["model_version"],
            int(protect_active_safe),
            "Operational group policy materialization; does not update ml_score_items, confirmations, models, or output files.",
            started_at.isoformat(timespec="seconds"),
            started_at.isoformat(timespec="seconds"),
        ),
    )
    return int(cursor.lastrowid)


def insert_policy_items(conn, policy_run_id: int, score_run_id: int, rows: list[dict[str, Any]], created_at: str) -> None:
    conn.executemany(
        """
        INSERT OR IGNORE INTO ml_policy_items (
            run_id,
            score_item_id,
            score_run_id,
            segment_id,
            relative_path,
            source_key,
            policy_group,
            policy_threshold,
            policy_require_learned_positive,
            score_final_action,
            policy_action,
            new_safe,
            demoted_safe,
            learned_positive,
            learned_negative,
            model_safe_probability,
            reasons_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                policy_run_id,
                row["score_item_id"],
                score_run_id,
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["policy_group"],
                row["policy_threshold"],
                int(row["policy_require_learned_positive"]),
                row["final_action"],
                row["policy_action"],
                int(row["new_safe"]),
                int(row["demoted_safe"]),
                int(row.get("learned_positive") or 0),
                int(row.get("learned_negative") or 0),
                row.get("model_safe_probability"),
                json.dumps(row["policy_reasons"], ensure_ascii=False),
                created_at,
            )
            for row in rows
        ],
    )


def update_policy_run_summary(
    conn,
    policy_run_id: int,
    scored_count: int,
    active_auto_safe_count: int,
    policy_auto_safe_count: int,
    new_safe_count: int,
    demoted_safe_count: int,
    finished_at: str,
) -> None:
    conn.execute(
        """
        UPDATE ml_policy_runs
        SET scored_count = ?,
            active_auto_safe_count = ?,
            policy_auto_safe_count = ?,
            new_safe_count = ?,
            demoted_safe_count = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            scored_count,
            active_auto_safe_count,
            policy_auto_safe_count,
            new_safe_count,
            demoted_safe_count,
            finished_at,
            finished_at,
            policy_run_id,
        ),
    )


def write_csv(settings: dict, rows: list[dict[str, Any]]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"{timestamp}_ml_group_threshold_policy_new_safe.csv"
    fieldnames = [
        "policy_group",
        "policy_threshold",
        "policy_require_learned_positive",
        "segment_id",
        "relative_path",
        "source_key",
        "model_safe_probability",
        "final_action",
        "learned_positive",
        "learned_negative",
        "candidate_text",
        "output_text",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    return path


def sample_lines(rows: list[dict[str, Any]], limit: int) -> list[str]:
    if not rows:
        return ["- none"]
    lines = []
    for row in rows[:limit]:
        lines.append(
            f"- {row['policy_group']} | threshold={row['policy_threshold']:.2f} | "
            f"safe_prob={row['model_safe_probability']:.4f} | "
            f"{row['relative_path']}::{row['source_key']} | candidate=\"{row.get('candidate_text') or ''}\""
        )
    return lines


def main(score_run_id: int | None = None, sample_limit: int = 60, protect_active_safe: bool = True) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[ml_group_threshold_policy] Starting group threshold simulation")
    print(f"[ml_group_threshold_policy] Rule version: {RULE_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        score_run_id = score_run_id or latest_score_run_id(conn)
        score_run = load_score_run(conn, score_run_id)
        rows = fetch_items(conn, score_run_id)

    policy_counts: Counter = Counter()
    active_counts: Counter = Counter()
    new_safe: list[dict[str, Any]] = []
    demoted_safe: list[dict[str, Any]] = []
    policy_rows: list[dict[str, Any]] = []
    group_stats: dict[str, Counter] = {}

    for row in rows:
        policy_group, threshold, rule = policy_for(row)
        row["policy_group"] = policy_group
        row["policy_threshold"] = threshold
        row["policy_require_learned_positive"] = bool(rule.get("require_learned_positive"))
        active_safe = row.get("final_action") == "auto_safe"
        effective_rule = dict(rule)
        if active_safe and protect_active_safe:
            effective_rule["require_learned_positive"] = False
        raw_policy_safe = policy_allows_safe(
            row,
            threshold,
            rule=effective_rule,
            enforce_language_blockers=not (protect_active_safe and active_safe),
        )
        policy_safe = (active_safe or raw_policy_safe) if protect_active_safe else raw_policy_safe
        row["policy_action"] = "auto_safe" if policy_safe else row["final_action"]
        row["new_safe"] = raw_policy_safe and not active_safe
        row["demoted_safe"] = active_safe and not raw_policy_safe
        row["policy_reasons"] = policy_reasons(
            row,
            rule,
            active_safe,
            raw_policy_safe,
            policy_safe,
            protect_active_safe,
        )
        active_counts["auto_safe" if active_safe else "not_safe"] += 1
        policy_counts["auto_safe" if policy_safe else "not_safe"] += 1
        stats = group_stats.setdefault(policy_group, Counter())
        stats["total"] += 1
        stats["active_safe"] += int(active_safe)
        stats["policy_safe"] += int(policy_safe)
        policy_rows.append(row)
        if row["new_safe"]:
            stats["new_safe"] += 1
            stats["new_safe_learned_positive"] += int(row.get("learned_positive") or 0)
            new_safe.append(row)
        if row["demoted_safe"]:
            stats["demoted_safe"] += 1
            demoted_safe.append(row)

    new_safe.sort(key=lambda item: (item["policy_group"], -float(item["model_safe_probability"] or 0), item["segment_id"]))
    demoted_safe.sort(key=lambda item: (item["policy_group"], -float(item["model_safe_probability"] or 0), item["segment_id"]))
    total = len(rows)
    csv_path = write_csv(settings, new_safe)
    finished_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        policy_run_id = insert_policy_run(conn, score_run, started_at, protect_active_safe)
        insert_policy_items(conn, policy_run_id, score_run_id, policy_rows, finished_at)
        update_policy_run_summary(
            conn,
            policy_run_id,
            total,
            active_counts["auto_safe"],
            policy_counts["auto_safe"],
            len(new_safe),
            len(demoted_safe),
            finished_at,
        )

    elapsed = datetime.now() - started_at
    report_lines = [
        "ML group threshold policy simulation",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Score run id: {score_run_id}",
        f"Model run id: {score_run['model_run_id']}",
        f"Model version: {score_run['model_version']}",
        f"Policy run id: {policy_run_id}",
        f"Rows evaluated: {total}",
        f"Default threshold: {DEFAULT_THRESHOLD:.2f}",
        f"Protect active safe: {protect_active_safe}",
        f"CSV new safe candidates: {csv_path}",
        "",
        "Policy rules:",
        *[
            "- "
            + rule["name"]
            + ": threshold="
            + f"{float(rule['threshold']):.2f}"
            + "".join(f", {key}={value}" for key, value in rule.items() if key not in {"name", "threshold"})
            for rule in FAMILY_POLICY_RULES
        ],
        *[
            "- "
            + rule["name"]
            + ": threshold="
            + f"{float(rule['threshold']):.2f}"
            + "".join(f", {key}={value}" for key, value in rule.items() if key not in {"name", "threshold"})
            for rule in POLICY_RULES
        ],
        "",
        "Summary:",
        f"- active auto_safe: {active_counts['auto_safe']} ({percent(active_counts['auto_safe'], total)})",
        f"- policy auto_safe: {policy_counts['auto_safe']} ({percent(policy_counts['auto_safe'], total)})",
        f"- new safe candidates: {len(new_safe)}",
        f"- active safe below stricter policy threshold: {len(demoted_safe)}",
        "",
        "By policy group:",
    ]
    for group, stats in sorted(
        group_stats.items(),
        key=lambda item: (-item[1]["new_safe"], item[0]),
    ):
        report_lines.extend(
            [
                f"- {group}:",
                f"  - total: {stats['total']}",
                f"  - active safe: {stats['active_safe']} ({percent(stats['active_safe'], stats['total'])})",
                f"  - policy safe: {stats['policy_safe']} ({percent(stats['policy_safe'], stats['total'])})",
                f"  - new safe: {stats['new_safe']}",
                f"  - new safe with learned positive: {stats['new_safe_learned_positive']}",
                f"  - active safe below stricter threshold: {stats['demoted_safe']}",
            ]
        )
    report_lines.extend(
        [
            "",
            f"Top {min(sample_limit, len(new_safe))} new safe candidates:",
            *sample_lines(new_safe, sample_limit),
            "",
            f"Top {min(sample_limit, len(demoted_safe))} active-safe candidates below stricter threshold:",
            *sample_lines(demoted_safe, sample_limit),
            "",
            "Interpretation:",
            "- This is an experimental simulation only; it does not update ml_score_items, confirmations, models, or output files.",
            "- The policy decisions were materialized in ml_policy_runs/ml_policy_items for dashboard and audit use.",
            "- New safe candidates need review before any group-specific threshold can become operational.",
            "- Active-safe rows below stricter thresholds are audit candidates only when protect_active_safe is enabled.",
        ]
    )
    report_path = db.write_report(settings, "ml_group_threshold_policy", report_lines)
    print(f"[ml_group_threshold_policy] Active auto_safe: {active_counts['auto_safe']}")
    print(f"[ml_group_threshold_policy] Policy auto_safe: {policy_counts['auto_safe']}")
    print(f"[ml_group_threshold_policy] New safe candidates: {len(new_safe)}")
    print(f"[ml_group_threshold_policy] Active safe below stricter threshold: {len(demoted_safe)}")
    print(f"[ml_group_threshold_policy] Policy run id: {policy_run_id}")
    print(f"[ml_group_threshold_policy] CSV: {csv_path}")
    print(f"[ml_group_threshold_policy] Report: {report_path}")
    print("[ml_group_threshold_policy] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulate experimental group/key-shape thresholds over a score run.")
    parser.add_argument("--score-run-id", type=int, default=None)
    parser.add_argument("--sample-limit", type=int, default=60)
    parser.add_argument("--allow-demotion", action="store_true")
    args = parser.parse_args()
    main(score_run_id=args.score_run_id, sample_limit=args.sample_limit, protect_active_safe=not args.allow_demotion)
