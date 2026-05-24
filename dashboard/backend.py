from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "memory" / "translation_engine.sqlite"


def _dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row else {}


def _one(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    return _dict(con.execute(sql, params).fetchone())


def _all(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute(sql, params).fetchall()]


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


def _int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    return int(value)


def _pct(part: Any, total: Any) -> float:
    total_num = _num(total)
    if total_num <= 0:
        return 0.0
    return round((_num(part) / total_num) * 100, 2)


def _short_model_name(version: str | None) -> str:
    if not version:
        return "sem modelo"
    if "_v" in version:
        return "v" + version.split("_v", 1)[1].split("_", 1)[0]
    return version


def _model_axis_label(row: dict[str, Any]) -> str:
    return f"#{row['id']} {_short_model_name(row.get('model_version'))}"


def _run_axis_label(run_id: Any) -> str:
    return str(_int(run_id))


def _latest_score(con: sqlite3.Connection, offset: int = 0, operational: bool = False) -> dict[str, Any]:
    where = "WHERE scored_count >= 10000" if operational else ""
    return _one(
        con,
        f"""
        SELECT *
        FROM ml_score_runs
        {where}
        ORDER BY id DESC
        LIMIT 1 OFFSET ?
        """,
        (offset,),
    )


def _active_model(con: sqlite3.Connection) -> dict[str, Any]:
    return _one(
        con,
        """
        SELECT
          r.active_model_run_id,
          r.active_model_version,
          r.policy_version,
          r.promoted_at,
          r.reason,
          m.*
        FROM ml_model_registry r
        LEFT JOIN ml_model_runs m ON m.id = r.active_model_run_id
        WHERE r.model_kind = 'risk_action_classifier'
        LIMIT 1
        """,
    )


def _latest_model(con: sqlite3.Connection) -> dict[str, Any]:
    return _one(
        con,
        """
        SELECT *
        FROM ml_model_runs
        WHERE model_kind = 'risk_action_classifier'
        ORDER BY id DESC
        LIMIT 1
        """,
    )


def _latest_dataset(con: sqlite3.Connection) -> dict[str, Any]:
    return _one(con, "SELECT * FROM ml_dataset_runs ORDER BY id DESC LIMIT 1")


def _dataset_negative_coverage(con: sqlite3.Connection, dataset_run_id: Any) -> float:
    row = _one(
        con,
        """
        SELECT negative_count, total_count
        FROM ml_dataset_runs
        WHERE id = ?
        """,
        (_int(dataset_run_id),),
    )
    return round(_pct(row.get("negative_count"), row.get("total_count")) / 100, 4)


def _policy_payload(con: sqlite3.Connection) -> dict[str, Any]:
    exists = _one(
        con,
        """
        SELECT COUNT(*) AS total
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN ('ml_policy_runs', 'ml_policy_items')
        """,
    )
    if _int(exists.get("total")) < 2:
        return {"available": False, "message": "Nenhuma politica operacional executada ainda"}

    summary = _one(
        con,
        """
        SELECT
          pr.id AS policy_run_id,
          pr.rule_version,
          pr.score_run_id,
          pr.model_run_id,
          pr.model_version,
          pr.scored_count,
          pr.active_auto_safe_count,
          pr.policy_auto_safe_count,
          pr.new_safe_count,
          pr.demoted_safe_count,
          pr.protect_active_safe,
          ROUND(100.0 * pr.active_auto_safe_count / NULLIF(pr.scored_count, 0), 2) AS active_auto_safe_pct,
          ROUND(100.0 * pr.policy_auto_safe_count / NULLIF(pr.scored_count, 0), 2) AS policy_auto_safe_pct,
          ROUND(100.0 * pr.new_safe_count / NULLIF(pr.scored_count, 0), 4) AS new_safe_pct,
          pr.started_at,
          pr.finished_at
        FROM ml_policy_runs pr
        ORDER BY pr.id DESC
        LIMIT 1
        """,
    )
    if not summary:
        return {"available": False, "message": "Nenhuma politica operacional executada ainda"}

    policy_run_id = _int(summary.get("policy_run_id"))
    comparison_rows = _all(
        con,
        """
        WITH score_counts AS (
          SELECT
            'score' AS source,
            msi.final_action AS action,
            COUNT(*) AS total
          FROM ml_policy_runs pr
          JOIN ml_score_items msi ON msi.run_id = pr.score_run_id
          WHERE pr.id = ?
          GROUP BY msi.final_action
        ),
        policy_counts AS (
          SELECT
            'policy' AS source,
            mpi.policy_action AS action,
            COUNT(*) AS total
          FROM ml_policy_items mpi
          WHERE mpi.run_id = ?
          GROUP BY mpi.policy_action
        )
        SELECT source, action, total
        FROM score_counts
        UNION ALL
        SELECT source, action, total
        FROM policy_counts
        ORDER BY source, action
        """,
        (policy_run_id, policy_run_id),
    )
    action_order = ["auto_safe", "needs_human", "needs_autofix", "blocked_structure"]
    comparison_by_action = {
        action: {"action": action, "score": 0, "policy": 0}
        for action in action_order
    }
    for row in comparison_rows:
        action = row.get("action")
        if action not in comparison_by_action:
            comparison_by_action[action] = {"action": action, "score": 0, "policy": 0}
        comparison_by_action[action][row["source"]] = _int(row.get("total"))
    comparison = [comparison_by_action[action] for action in comparison_by_action]

    group_gain = _all(
        con,
        """
        SELECT
          policy_group,
          COUNT(*) AS total_segments,
          SUM(CASE WHEN score_final_action = 'auto_safe' THEN 1 ELSE 0 END) AS score_auto_safe,
          SUM(CASE WHEN policy_action = 'auto_safe' THEN 1 ELSE 0 END) AS policy_auto_safe,
          SUM(new_safe) AS new_safe,
          SUM(CASE WHEN new_safe = 1 AND learned_positive = 1 THEN 1 ELSE 0 END) AS new_safe_learned_positive,
          SUM(CASE WHEN learned_negative = 1 THEN 1 ELSE 0 END) AS learned_negative_count,
          MIN(policy_threshold) AS min_threshold,
          MAX(policy_threshold) AS max_threshold,
          ROUND(100.0 * SUM(new_safe) / NULLIF(COUNT(*), 0), 4) AS new_safe_pct_in_group,
          ROUND(100.0 * SUM(CASE WHEN policy_action = 'auto_safe' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 2) AS policy_auto_safe_pct
        FROM ml_policy_items
        WHERE run_id = ?
        GROUP BY policy_group
        ORDER BY new_safe DESC, policy_group
        LIMIT 12
        """,
        (policy_run_id,),
    )

    audit_items = _all(
        con,
        """
        SELECT
          mpi.segment_id,
          mpi.policy_group,
          mpi.relative_path,
          mpi.source_key,
          mpi.model_safe_probability,
          mpi.policy_threshold,
          mpi.policy_require_learned_positive,
          mpi.score_final_action,
          mpi.policy_action,
          mpi.learned_positive,
          mpi.learned_negative,
          msi.candidate_text,
          os.portuguese_text AS output_text,
          mpi.reasons_json
        FROM ml_policy_items mpi
        JOIN ml_score_items msi ON msi.id = mpi.score_item_id
        LEFT JOIN output_segments os ON os.segment_id = mpi.segment_id
        WHERE mpi.run_id = ?
          AND mpi.new_safe = 1
        ORDER BY mpi.policy_group, mpi.model_safe_probability DESC, mpi.segment_id
        LIMIT 120
        """,
        (policy_run_id,),
    )

    history = _all(
        con,
        """
        SELECT
          id AS policy_run_id,
          rule_version,
          score_run_id,
          model_version,
          scored_count,
          active_auto_safe_count,
          policy_auto_safe_count,
          new_safe_count,
          demoted_safe_count,
          ROUND(100.0 * policy_auto_safe_count / NULLIF(scored_count, 0), 2) AS policy_auto_safe_pct,
          started_at,
          finished_at
        FROM ml_policy_runs
        ORDER BY id ASC
        """,
    )

    return {
        "available": True,
        "summary": summary,
        "comparison": comparison,
        "groupGain": group_gain,
        "auditItems": audit_items,
        "history": history,
    }


def _lab_payload(con: sqlite3.Connection) -> dict[str, Any]:
    summary = _one(
        con,
        """
        WITH active_registry AS (
          SELECT active_model_run_id
          FROM ml_model_registry
          WHERE model_kind = 'risk_action_classifier'
          LIMIT 1
        ),
        candidate_model AS (
          SELECT *
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        active_model AS (
          SELECT m.*
          FROM ml_model_runs m
          JOIN active_registry ar ON ar.active_model_run_id = m.id
        ),
        candidate_score AS (
          SELECT *
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        ),
        active_score AS (
          SELECT *
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM active_model)
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_promotion AS (
          SELECT *
          FROM ml_model_promotions
          WHERE candidate_model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        )
        SELECT
          cm.id AS candidate_model_run_id,
          cm.model_version AS candidate_model_version,
          cm.dataset_run_id AS candidate_dataset_run_id,
          cm.accuracy AS candidate_accuracy,
          cm.macro_f1 AS candidate_macro_f1,
          cm.false_safe_count AS candidate_false_safe_count,
          cm.false_safe_rate AS candidate_false_safe_rate,
          cm.safe_precision AS candidate_safe_precision,
          cm.safe_recall AS candidate_safe_recall,
          cm.safe_threshold AS candidate_safe_threshold,
          cs.id AS candidate_score_run_id,
          cs.scored_count AS candidate_scored_count,
          cs.final_auto_safe_count AS candidate_auto_safe_count,
          ROUND(100.0 * cs.final_auto_safe_count / NULLIF(cs.scored_count, 0), 2) AS candidate_auto_safe_pct,
          am.id AS active_model_run_id,
          am.model_version AS active_model_version,
          am.safe_precision AS active_safe_precision,
          am.safe_recall AS active_safe_recall,
          am.macro_f1 AS active_macro_f1,
          ast.id AS active_score_run_id,
          ast.scored_count AS active_scored_count,
          ast.final_auto_safe_count AS active_auto_safe_count,
          ROUND(100.0 * ast.final_auto_safe_count / NULLIF(ast.scored_count, 0), 2) AS active_auto_safe_pct,
          cp.id AS promotion_run_id,
          cp.decision AS promotion_decision,
          cp.policy_version AS promotion_policy_version,
          cp.reason AS promotion_reason,
          cp.created_at AS promotion_created_at
        FROM candidate_model cm
        CROSS JOIN active_model am
        LEFT JOIN candidate_score cs ON 1 = 1
        LEFT JOIN active_score ast ON 1 = 1
        LEFT JOIN candidate_promotion cp ON 1 = 1
        """,
    )
    if not summary:
        return {"available": False, "message": "Nenhum modelo experimental encontrado"}

    bars_rows = _all(
        con,
        """
        WITH active_registry AS (
          SELECT active_model_run_id
          FROM ml_model_registry
          WHERE model_kind = 'risk_action_classifier'
          LIMIT 1
        ),
        candidate_model AS (
          SELECT id
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        active_score AS (
          SELECT *
          FROM ml_score_runs
          WHERE model_run_id = (SELECT active_model_run_id FROM active_registry)
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_score AS (
          SELECT *
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        )
        SELECT
          'active' AS model_role,
          id AS score_run_id,
          model_run_id,
          scored_count,
          final_auto_safe_count AS auto_safe,
          needs_human_count AS needs_human,
          needs_autofix_count AS needs_autofix,
          blocked_structure_count AS blocked_structure,
          deterministic_block_count AS deterministic_blocks,
          ROUND(100.0 * final_auto_safe_count / NULLIF(scored_count, 0), 2) AS auto_safe_pct
        FROM active_score
        UNION ALL
        SELECT
          'candidate' AS model_role,
          id AS score_run_id,
          model_run_id,
          scored_count,
          final_auto_safe_count AS auto_safe,
          needs_human_count AS needs_human,
          needs_autofix_count AS needs_autofix,
          blocked_structure_count AS blocked_structure,
          deterministic_block_count AS deterministic_blocks,
          ROUND(100.0 * final_auto_safe_count / NULLIF(scored_count, 0), 2) AS auto_safe_pct
        FROM candidate_score
        """,
    )
    role_rows = {row["model_role"]: row for row in bars_rows}
    action_comparison = []
    for key, label in [
        ("auto_safe", "Auto-safe"),
        ("needs_human", "Human"),
        ("needs_autofix", "Autofix"),
        ("blocked_structure", "Blocked"),
    ]:
        action_comparison.append(
            {
                "action": label,
                "active": _int(role_rows.get("active", {}).get(key)),
                "candidate": _int(role_rows.get("candidate", {}).get(key)),
            }
        )

    recent_models = _all(
        con,
        """
        WITH latest_score_by_model AS (
          SELECT model_run_id, MAX(id) AS latest_score_run_id
          FROM ml_score_runs
          GROUP BY model_run_id
        ),
        latest_promotion_by_model AS (
          SELECT candidate_model_run_id, MAX(id) AS latest_promotion_id
          FROM ml_model_promotions
          GROUP BY candidate_model_run_id
        )
        SELECT
          m.id AS model_run_id,
          m.model_version,
          m.dataset_run_id,
          m.safe_threshold,
          m.accuracy,
          m.macro_f1,
          m.false_safe_count,
          m.false_safe_rate,
          m.safe_precision,
          m.safe_recall,
          s.id AS latest_score_run_id,
          s.scored_count,
          s.final_auto_safe_count,
          ROUND(100.0 * s.final_auto_safe_count / NULLIF(s.scored_count, 0), 2) AS operational_auto_safe_pct,
          p.decision AS latest_promotion_decision,
          p.reason AS latest_promotion_reason,
          m.started_at,
          m.finished_at
        FROM ml_model_runs m
        LEFT JOIN latest_score_by_model lsm ON lsm.model_run_id = m.id
        LEFT JOIN ml_score_runs s ON s.id = lsm.latest_score_run_id
        LEFT JOIN latest_promotion_by_model lpm ON lpm.candidate_model_run_id = m.id
        LEFT JOIN ml_model_promotions p ON p.id = lpm.latest_promotion_id
        WHERE m.model_kind = 'risk_action_classifier'
        ORDER BY m.id DESC
        LIMIT 30
        """,
    )
    recent_models.reverse()

    candidate_distribution = _all(
        con,
        """
        WITH candidate_model AS (
          SELECT id
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        )
        SELECT
          final_action,
          risk_class,
          COUNT(*) AS total,
          ROUND(AVG(model_safe_probability), 4) AS avg_safe_probability,
          SUM(CASE WHEN token_status <> 'ok' THEN 1 ELSE 0 END) AS token_mismatch_count,
          SUM(CASE WHEN deterministic_blocked = 1 THEN 1 ELSE 0 END) AS deterministic_blocked_count
        FROM ml_score_items
        WHERE run_id = (SELECT id FROM candidate_score)
        GROUP BY final_action, risk_class
        ORDER BY total DESC
        """,
    )

    file_regressions = _all(
        con,
        """
        WITH active_registry AS (
          SELECT active_model_run_id
          FROM ml_model_registry
          WHERE model_kind = 'risk_action_classifier'
          LIMIT 1
        ),
        candidate_model AS (
          SELECT id
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        active_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT active_model_run_id FROM active_registry)
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        ),
        joined AS (
          SELECT
            a.segment_id,
            a.relative_path,
            a.source_key,
            a.final_action AS active_action,
            c.final_action AS candidate_action,
            c.model_safe_probability AS candidate_safe_probability
          FROM ml_score_items a
          JOIN ml_score_items c ON c.segment_id = a.segment_id
          WHERE a.run_id = (SELECT id FROM active_score)
            AND c.run_id = (SELECT id FROM candidate_score)
        )
        SELECT
          relative_path,
          COUNT(*) AS compared_segments,
          SUM(CASE WHEN active_action = 'auto_safe' AND candidate_action <> 'auto_safe' THEN 1 ELSE 0 END) AS operational_regressions,
          SUM(CASE WHEN active_action <> 'auto_safe' AND candidate_action = 'auto_safe' THEN 1 ELSE 0 END) AS candidate_recoveries,
          SUM(CASE WHEN active_action = 'auto_safe' AND candidate_action = 'auto_safe' THEN 1 ELSE 0 END) AS both_auto_safe,
          SUM(CASE WHEN active_action <> 'auto_safe' AND candidate_action <> 'auto_safe' THEN 1 ELSE 0 END) AS both_not_safe,
          ROUND(AVG(candidate_safe_probability), 4) AS avg_candidate_safe_probability
        FROM joined
        GROUP BY relative_path
        ORDER BY operational_regressions DESC, candidate_recoveries DESC
        LIMIT 50
        """,
    )

    regression_audit = _all(
        con,
        """
        WITH active_registry AS (
          SELECT active_model_run_id
          FROM ml_model_registry
          WHERE model_kind = 'risk_action_classifier'
          LIMIT 1
        ),
        candidate_model AS (
          SELECT id
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        active_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT active_model_run_id FROM active_registry)
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        )
        SELECT
          c.segment_id,
          c.relative_path,
          c.source_key,
          a.final_action AS active_action,
          c.final_action AS candidate_action,
          c.model_safe_probability AS candidate_safe_probability,
          c.risk_class AS candidate_risk_class,
          c.token_status,
          c.issue_count,
          c.deterministic_blocked,
          c.candidate_text,
          c.reasons_json
        FROM ml_score_items a
        JOIN ml_score_items c ON c.segment_id = a.segment_id
        WHERE a.run_id = (SELECT id FROM active_score)
          AND c.run_id = (SELECT id FROM candidate_score)
          AND a.final_action = 'auto_safe'
          AND c.final_action <> 'auto_safe'
        ORDER BY c.model_safe_probability DESC, c.relative_path, c.source_key
        LIMIT 500
        """,
    )

    return {
        "available": True,
        "summary": summary,
        "actionComparison": action_comparison,
        "recentModels": recent_models,
        "candidateDistribution": candidate_distribution,
        "fileRegressions": file_regressions,
        "regressionAudit": regression_audit,
        "gaps": {
            "auto_safe_gap_count": _int(summary.get("active_auto_safe_count")) - _int(summary.get("candidate_auto_safe_count")),
            "auto_safe_gap_pct_points": round(_num(summary.get("active_auto_safe_pct")) - _num(summary.get("candidate_auto_safe_pct")), 2),
        },
    }


def _specialists_payload(con: sqlite3.Connection) -> dict[str, Any]:
    latest_policy = _one(
        con,
        """
        SELECT *
        FROM ml_policy_runs
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    has_specialist_models = _int(
        _one(
            con,
            """
            SELECT COUNT(*) AS total
            FROM ml_model_runs
            WHERE lower(model_kind) LIKE '%special%'
               OR lower(model_version) LIKE '%special%'
            """,
        ).get("total")
    )

    specialists = _all(
        con,
        """
        SELECT
          m.id AS model_run_id,
          m.model_version AS specialist_name,
          m.model_kind AS scope,
          m.dataset_run_id,
          'experimental' AS status,
          m.training_examples AS examples_total,
          d.positive_count AS positive_examples,
          d.negative_count AS negative_examples,
          m.safe_precision,
          m.safe_recall,
          m.macro_f1,
          m.false_safe_count AS false_safe,
          ROUND(100.0 * COALESCE(s.final_auto_safe_count, 0) / NULLIF(s.scored_count, 0), 2) AS coverage_pct,
          m.finished_at
        FROM ml_model_runs m
        LEFT JOIN ml_dataset_runs d ON d.id = m.dataset_run_id
        LEFT JOIN (
          SELECT model_run_id, MAX(id) AS latest_score_run_id
          FROM ml_score_runs
          GROUP BY model_run_id
        ) latest_score ON latest_score.model_run_id = m.id
        LEFT JOIN ml_score_runs s ON s.id = latest_score.latest_score_run_id
        WHERE lower(m.model_kind) LIKE '%special%'
           OR lower(m.model_version) LIKE '%special%'
        ORDER BY m.id DESC
        LIMIT 20
        """,
    )

    policy_run_id = _int(latest_policy.get("id"))
    group_comparison = []
    divergence_matrix = []
    audit_queue = []
    if policy_run_id:
        group_comparison = _all(
            con,
            """
            SELECT
              policy_group AS group_name,
              COUNT(*) AS total,
              SUM(CASE WHEN score_final_action = 'auto_safe' THEN 1 ELSE 0 END) AS general_auto_safe,
              SUM(CASE WHEN policy_action = 'auto_safe' THEN 1 ELSE 0 END) AS policy_auto_safe,
              NULL AS specialist_auto_safe,
              SUM(CASE WHEN policy_action = 'auto_safe' THEN 1 ELSE 0 END) AS auditor_auto_safe,
              SUM(new_safe) AS auditor_new_safe,
              SUM(demoted_safe) AS auditor_demoted_safe,
              0 AS false_safe,
              SUM(CASE WHEN learned_positive = 1 THEN 1 ELSE 0 END) AS human_positive,
              SUM(CASE WHEN learned_negative = 1 THEN 1 ELSE 0 END) AS human_negative
            FROM ml_policy_items
            WHERE run_id = ?
            GROUP BY policy_group
            ORDER BY auditor_new_safe DESC, auditor_demoted_safe DESC, policy_group
            LIMIT 20
            """,
            (policy_run_id,),
        )
        divergence_matrix = _all(
            con,
            """
            SELECT category, count, risk_level, recommended_action
            FROM (
              SELECT
                'auditor_new_safe' AS category,
                SUM(new_safe) AS count,
                'review' AS risk_level,
                'human_audit_queue' AS recommended_action
              FROM ml_policy_items
              WHERE run_id = ?
              UNION ALL
              SELECT
                'auditor_demoted_safe' AS category,
                SUM(demoted_safe) AS count,
                'risk' AS risk_level,
                'inspect_policy_group' AS recommended_action
              FROM ml_policy_items
              WHERE run_id = ?
              UNION ALL
              SELECT
                'learned_negative_blocked' AS category,
                SUM(CASE WHEN learned_negative = 1 AND policy_action <> 'auto_safe' THEN 1 ELSE 0 END) AS count,
                'safe' AS risk_level,
                'keep_blocked' AS recommended_action
              FROM ml_policy_items
              WHERE run_id = ?
            )
            WHERE count > 0
            ORDER BY count DESC
            """,
            (policy_run_id, policy_run_id, policy_run_id),
        )
        audit_queue = _all(
            con,
            """
            SELECT
              mpi.segment_id,
              mpi.relative_path,
              mpi.source_key,
              mpi.policy_group AS group_name,
              ss.english_text,
              os.portuguese_text AS current_output_text,
              mpi.score_final_action AS general_action,
              NULL AS specialist_action,
              mpi.policy_action AS auditor_action,
              mpi.reasons_json AS reason,
              mpi.model_safe_probability AS general_probability,
              NULL AS specialist_probability
            FROM ml_policy_items mpi
            LEFT JOIN source_segments ss ON ss.id = mpi.segment_id
            LEFT JOIN output_segments os ON os.segment_id = mpi.segment_id
            WHERE mpi.run_id = ?
              AND (mpi.new_safe = 1 OR mpi.demoted_safe = 1 OR mpi.learned_negative = 1)
            ORDER BY mpi.demoted_safe DESC, mpi.new_safe DESC, mpi.model_safe_probability DESC
            LIMIT 120
            """,
            (policy_run_id,),
        )

    evolution = _all(
        con,
        """
        SELECT
          id AS run_id,
          started_at,
          ROUND(100.0 * active_auto_safe_count / NULLIF(scored_count, 0), 2) AS general_auto_safe_pct,
          NULL AS specialist_auto_safe_pct,
          ROUND(100.0 * policy_auto_safe_count / NULLIF(scored_count, 0), 2) AS auditor_auto_safe_pct,
          0 AS false_safe,
          new_safe_count + demoted_safe_count AS disagreements,
          (
            SELECT COUNT(*)
            FROM local_learning_candidates
            WHERE human_label <> 'pending'
          ) AS human_examples_total
        FROM ml_policy_runs
        ORDER BY id ASC
        """,
    )

    return {
        "summary": {
            "specialists_total": has_specialist_models,
            "specialists_active": 0,
            "auditor_auto_safe_count": _int(latest_policy.get("policy_auto_safe_count")),
            "auditor_auto_safe_pct": _pct(latest_policy.get("policy_auto_safe_count"), latest_policy.get("scored_count")),
            "open_disagreements": _int(latest_policy.get("new_safe_count")) + _int(latest_policy.get("demoted_safe_count")),
            "specialist_false_safe": sum(_int(row.get("false_safe")) for row in specialists),
            "auditor_new_safe": _int(latest_policy.get("new_safe_count")),
        },
        "specialists": specialists,
        "groupComparison": group_comparison,
        "divergenceMatrix": divergence_matrix,
        "auditQueue": audit_queue,
        "evolution": evolution,
    }


def _dashboard_payload(db_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        segment_counts = _one(
            con,
            """
            SELECT
              COUNT(*) AS active_segments,
              SUM(CASE WHEN has_english = 1 THEN 1 ELSE 0 END) AS with_english,
              SUM(CASE WHEN has_old = 1 THEN 1 ELSE 0 END) AS with_old
            FROM source_segments
            WHERE is_active = 1
            """,
        )
        output_counts = _one(
            con,
            """
            SELECT
              SUM(CASE WHEN COALESCE(o.portuguese_text, '') <> '' THEN 1 ELSE 0 END) AS with_output,
              SUM(CASE WHEN COALESCE(o.portuguese_text, '') = '' THEN 1 ELSE 0 END) AS without_output
            FROM source_segments s
            LEFT JOIN output_segments o ON o.segment_id = s.id
            WHERE s.is_active = 1
            """,
        )
        confirmation_counts = _one(
            con,
            """
            SELECT
              COUNT(DISTINCT sc.segment_id) AS total,
              COUNT(DISTINCT CASE WHEN sc.locked = 1 THEN sc.segment_id END) AS locked,
              COUNT(DISTINCT CASE WHEN sc.locked = 1 AND sc.confirmation_level IN ('human_confirmed', 'human') THEN sc.segment_id END) AS locked_human,
              COUNT(DISTINCT CASE WHEN sc.confirmation_level = 'auto_confirmed' THEN sc.segment_id END) AS auto_confirmed,
              COUNT(DISTINCT CASE WHEN sc.locked = 0 AND sc.confirmation_level IN ('human_confirmed', 'human') THEN sc.segment_id END) AS human_unlocked
            FROM segment_confirmations sc
            JOIN source_segments s ON s.id = sc.segment_id
            WHERE s.is_active = 1
            """,
        )
        review_counts = _one(
            con,
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN human_label = 'pending' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN human_label <> 'pending' THEN 1 ELSE 0 END) AS reviewed
            FROM local_learning_candidates
            """,
        )
        issue_counts = _one(
            con,
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN severity IN ('high', 'critical') THEN 1 ELSE 0 END) AS high
            FROM issues
            """,
        )

        latest_score = _latest_score(con, operational=True) or _latest_score(con)
        previous_score = _latest_score(con, 1, operational=True) or _latest_score(con, 1)
        active_model = _active_model(con)
        latest_model = _latest_model(con)
        latest_dataset = _latest_dataset(con)

        scored = _int(latest_score.get("scored_count"))
        auto_safe = _int(latest_score.get("final_auto_safe_count"))
        pending = (
            _int(latest_score.get("needs_human_count"))
            + _int(latest_score.get("needs_autofix_count"))
            + _int(latest_score.get("blocked_structure_count"))
        )
        auto_safe_pct = _pct(auto_safe, scored)
        previous_auto_safe_pct = _pct(
            previous_score.get("final_auto_safe_count"),
            previous_score.get("scored_count"),
        )
        latest_score_id = _int(latest_score.get("id"))

        model_rows = _all(
            con,
            """
            SELECT id, model_version, macro_f1, safe_precision, safe_recall, false_safe_count, predicted_safe_count, started_at
            FROM ml_model_runs
            WHERE model_kind = 'risk_action_classifier'
            ORDER BY id DESC
            LIMIT 12
            """,
        )
        model_rows.reverse()
        ml_trend = [
            {
                "model": _run_axis_label(row["id"]),
                "modelVersion": row.get("model_version"),
                "modelLabel": _short_model_name(row.get("model_version")),
                "runId": row["id"],
                "macroF1": round(_num(row.get("macro_f1")), 4),
                "safePrecision": round(_num(row.get("safe_precision")), 4),
                "safeRecall": round(_num(row.get("safe_recall")), 4),
                "falseSafe": _int(row.get("false_safe_count")),
                "predictedSafe": _int(row.get("predicted_safe_count")),
            }
            for row in model_rows
        ]
        latest_by_model: dict[str, dict[str, Any]] = {}
        for row in ml_trend:
            key = row.get("modelLabel") or row.get("modelVersion") or str(row.get("runId"))
            if key not in latest_by_model or _int(row.get("runId")) > _int(latest_by_model[key].get("runId")):
                latest_by_model[key] = row
        ml_trend_by_model = [
            {
                **row,
                "model": row.get("modelLabel") or row.get("model"),
            }
            for row in sorted(latest_by_model.values(), key=lambda item: _int(item.get("runId")))
        ]

        score_rows = _all(
            con,
            """
            SELECT id, model_run_id, scored_count, final_auto_safe_count, needs_human_count, needs_autofix_count, blocked_structure_count, started_at
            FROM ml_score_runs
            WHERE scored_count >= 10000
            ORDER BY id DESC
            LIMIT 10
            """,
        )
        score_rows.reverse()
        score_model_rows = _all(
            con,
            """
            SELECT id, model_version, macro_f1
            FROM ml_model_runs
            WHERE id IN (
              SELECT DISTINCT model_run_id
              FROM ml_score_runs
              WHERE scored_count >= 10000
            )
            """,
        )
        score_model_by_id = {row["id"]: row for row in score_model_rows}
        quality_trend = []
        for row in score_rows:
            score_pending = _int(row.get("needs_human_count")) + _int(row.get("needs_autofix_count")) + _int(row.get("blocked_structure_count"))
            current_model = score_model_by_id.get(_int(row.get("model_run_id")), {})
            quality_index = round(_num(current_model.get("macro_f1")) * 100, 2) if current_model else 0
            quality_trend.append(
                {
                    "run": f"R{row['id']}",
                    "runLabel": _run_axis_label(row["id"]),
                    "modelRunId": row.get("model_run_id"),
                    "modelVersion": current_model.get("model_version"),
                    "qualityIndex": quality_index,
                    "autoSafe": _pct(row.get("final_auto_safe_count"), row.get("scored_count")),
                    "pending": score_pending,
                }
            )
        latest_score_by_model: dict[str, dict[str, Any]] = {}
        for row in quality_trend:
            key = _short_model_name(row.get("modelVersion")) or str(row.get("modelRunId"))
            if key not in latest_score_by_model or _int(row.get("modelRunId")) > _int(latest_score_by_model[key].get("modelRunId")):
                latest_score_by_model[key] = row
        quality_trend_by_model = [
            {
                **row,
                "runLabel": _short_model_name(row.get("modelVersion")),
            }
            for row in sorted(latest_score_by_model.values(), key=lambda item: _int(item.get("modelRunId")))
        ]

        dataset_composition = [
            {"label": "Positivos", "value": _int(latest_dataset.get("positive_count"))},
            {"label": "Negativos", "value": _int(latest_dataset.get("negative_count"))},
            {"label": "Neutros", "value": _int(latest_dataset.get("neutral_count"))},
            {"label": "Strong +", "value": _int(latest_dataset.get("strong_positive_count"))},
            {"label": "Strong -", "value": _int(latest_dataset.get("strong_negative_count"))},
        ]

        segment_distribution = [
            {"name": "Auto-safe", "value": auto_safe, "color": "#10b981"},
            {"name": "Revisao humana", "value": _int(latest_score.get("needs_human_count")), "color": "#f59e0b"},
            {"name": "Autofix", "value": _int(latest_score.get("needs_autofix_count")), "color": "#3b82f6"},
            {"name": "Bloqueio estrutural", "value": _int(latest_score.get("blocked_structure_count")), "color": "#ef4444"},
        ]

        pipeline_status = [
            {"status": "Sem output", "count": _int(output_counts.get("without_output"))},
            {
                "status": "Output nao confirmado",
                "count": max(_int(output_counts.get("with_output")) - _int(confirmation_counts.get("total")), 0),
            },
            {"status": "Confirmado automatico", "count": _int(confirmation_counts.get("auto_confirmed"))},
            {"status": "Confirmado humano", "count": _int(confirmation_counts.get("human_unlocked"))},
            {"status": "Locked humano", "count": _int(confirmation_counts.get("locked_human"))},
        ]

        package_backlog = _all(
            con,
            """
            SELECT
              relative_path AS file,
              SUM(CASE WHEN final_action <> 'auto_safe' THEN 1 ELSE 0 END) AS pending
            FROM ml_score_items
            WHERE run_id = ?
            GROUP BY relative_path
            HAVING pending > 0
            ORDER BY pending DESC
            LIMIT 8
            """,
            (latest_score_id,),
        )

        human_reviews = _all(
            con,
            """
            SELECT
              DATE(COALESCE(reviewed_at, updated_at, created_at)) AS day,
              SUM(CASE WHEN human_label = 'correct' THEN 1 ELSE 0 END) AS correct,
              SUM(CASE WHEN human_label = 'minor_fix' THEN 1 ELSE 0 END) AS minorFix,
              SUM(CASE WHEN human_label = 'semantic_error' THEN 1 ELSE 0 END) AS semanticError,
              SUM(CASE WHEN human_label = 'residual_spanish' THEN 1 ELSE 0 END) AS residualSpanish
            FROM local_learning_candidates
            WHERE human_label <> 'pending'
            GROUP BY DATE(COALESCE(reviewed_at, updated_at, created_at))
            ORDER BY day DESC
            LIMIT 7
            """,
        )
        human_reviews.reverse()

        promotion_rows = _all(
            con,
            """
            SELECT
              p.id,
              p.candidate_model_run_id,
              COALESCE(m.model_version, 'modelo ' || p.candidate_model_run_id) AS model_version,
              p.decision,
              COALESCE(m.false_safe_count, 0) AS false_safe_count,
              COALESCE(m.safe_recall, 0) AS safe_recall,
              COALESCE(m.safe_precision, 0) AS safe_precision
            FROM ml_model_promotions p
            LEFT JOIN ml_model_runs m ON m.id = p.candidate_model_run_id
            ORDER BY p.id DESC
            LIMIT 10
            """,
        )
        promotion_rows.reverse()
        promotion_timeline = [
            {
                "model": _run_axis_label(row["candidate_model_run_id"]),
                "modelVersion": row["model_version"],
                "decision": "Promovido" if row["decision"] == "promote" else "Rejeitado",
                "risk": _int(row["false_safe_count"]),
                "holdoutCoverage": round(_num(row.get("safe_recall")) * 100, 2),
                "safePrecision": round(_num(row.get("safe_precision")) * 100, 2),
            }
            for row in promotion_rows
        ]

        block_reasons = _all(
            con,
            """
            SELECT issue_type AS reason, COUNT(*) AS count
            FROM issues
            GROUP BY issue_type
            ORDER BY count DESC
            LIMIT 8
            """,
        )
        if not block_reasons:
            block_reasons = _all(
                con,
                """
                SELECT reason, SUM(count) AS count
                FROM (
                  SELECT 'Bloqueio estrutural' AS reason, COUNT(*) AS count
                  FROM ml_score_items
                  WHERE run_id = ? AND final_action = 'blocked_structure'
                  UNION ALL
                  SELECT 'Bloqueio determinístico' AS reason, COUNT(*) AS count
                  FROM ml_score_items
                  WHERE run_id = ? AND deterministic_blocked = 1
                  UNION ALL
                  SELECT 'Token diferente de ok' AS reason, COUNT(*) AS count
                  FROM ml_score_items
                  WHERE run_id = ? AND COALESCE(token_status, 'ok') <> 'ok'
                )
                GROUP BY reason
                HAVING count > 0
                ORDER BY count DESC
                """,
                (latest_score_id, latest_score_id, latest_score_id),
            )

        confirmation_sources = _all(
            con,
            """
            SELECT
              COALESCE(confirmation_source, confirmation_level, 'desconhecido') AS name,
              COUNT(*) AS value
            FROM segment_confirmations
            GROUP BY COALESCE(confirmation_source, confirmation_level, 'desconhecido')
            ORDER BY value DESC
            LIMIT 6
            """,
        )
        palette = ["#10b981", "#8b5cf6", "#3b82f6", "#f59e0b", "#14b8a6", "#ef4444"]
        for index, row in enumerate(confirmation_sources):
            row["color"] = palette[index % len(palette)]

        model_total_count = _int(
            _one(
                con,
                "SELECT COUNT(*) AS total FROM ml_model_runs WHERE model_kind = 'risk_action_classifier'",
            ).get("total")
        )
        promoted_count = _int(
            _one(
                con,
                """
                WITH latest_decision AS (
                  SELECT p.*
                  FROM ml_model_promotions p
                  JOIN (
                    SELECT candidate_model_run_id, MAX(id) AS latest_id
                    FROM ml_model_promotions
                    GROUP BY candidate_model_run_id
                  ) latest ON latest.latest_id = p.id
                )
                SELECT COUNT(*) AS total
                FROM latest_decision
                WHERE decision = 'promote'
                """,
            ).get("total")
        )
        rejected_count = _int(
            _one(
                con,
                """
                WITH latest_decision AS (
                  SELECT p.*
                  FROM ml_model_promotions p
                  JOIN (
                    SELECT candidate_model_run_id, MAX(id) AS latest_id
                    FROM ml_model_promotions
                    GROUP BY candidate_model_run_id
                  ) latest ON latest.latest_id = p.id
                )
                SELECT COUNT(*) AS total
                FROM latest_decision
                WHERE decision <> 'promote'
                """,
            ).get("total")
        )
        evaluated_model_count = _int(
            _one(
                con,
                "SELECT COUNT(DISTINCT candidate_model_run_id) AS total FROM ml_model_promotions",
            ).get("total")
        )
        unevaluated_model_count = max(model_total_count - evaluated_model_count, 0)

        active_precision = _num(active_model.get("safe_precision"))
        latest_precision = _num(latest_model.get("safe_precision"))
        candidate_delta = latest_precision - active_precision
        model_comparison = [
            {"metric": "Accuracy", "current": round(_num(active_model.get("accuracy")), 4), "candidate": round(_num(latest_model.get("accuracy")), 4), "format": "percent"},
            {"metric": "Macro F1", "current": round(_num(active_model.get("macro_f1")), 4), "candidate": round(_num(latest_model.get("macro_f1")), 4)},
            {"metric": "Safe Precision", "current": round(active_precision, 4), "candidate": round(latest_precision, 4)},
            {"metric": "Holdout Coverage", "current": round(_num(active_model.get("safe_recall")), 4), "candidate": round(_num(latest_model.get("safe_recall")), 4), "format": "percent"},
            {"metric": "False Safe", "current": _int(active_model.get("false_safe_count")), "candidate": _int(latest_model.get("false_safe_count"))},
            {"metric": "Predicted Safe", "current": _int(active_model.get("predicted_safe_count")), "candidate": _int(latest_model.get("predicted_safe_count"))},
        ]

        return {
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "databasePath": str(db_path),
            "cockpit": {
                "kpis": {
                    "activeSegments": _int(segment_counts.get("active_segments")),
                    "outputCoverage": _pct(output_counts.get("with_output"), segment_counts.get("active_segments")),
                    "autoSafeEfficiency": auto_safe_pct,
                    "autoSafeDelta": round(auto_safe_pct - previous_auto_safe_pct, 2),
                    "pendingReview": pending,
                },
                "qualityTrend": quality_trend,
                "qualityTrendByModel": quality_trend_by_model,
                "segmentDistribution": segment_distribution,
                "status": {
                    "activeModel": active_model.get("active_model_version"),
                    "activeModelRunId": active_model.get("active_model_run_id"),
                    "latestScoreRunId": latest_score.get("id"),
                    "latestDatasetRunId": latest_dataset.get("id"),
                },
            },
            "mlPerformance": {
                "kpis": {
                    "activeModelShort": _short_model_name(active_model.get("active_model_version")),
                    "activeModel": active_model.get("active_model_version"),
                    "macroF1": round(_num(active_model.get("macro_f1")), 4),
                    "safePrecision": round(active_precision, 4),
                    "holdoutCoverage": round(_num(active_model.get("safe_recall")), 4),
                    "negativeCoverage": _pct(latest_dataset.get("negative_count"), latest_dataset.get("total_count")),
                },
                "mlTrend": ml_trend,
                "mlTrendByModel": ml_trend_by_model,
                "datasetComposition": dataset_composition,
                "modelComparison": model_comparison,
                "candidateDecision": "promote" if latest_model.get("id") == active_model.get("active_model_run_id") else "review",
                "candidateDeltaSafePrecision": round(candidate_delta, 4),
            },
            "pipeline": {
                "kpis": {
                    "segmentsTotal": _int(segment_counts.get("active_segments")),
                    "withOutput": _int(output_counts.get("with_output")),
                    "withoutOutput": _int(output_counts.get("without_output")),
                    "lockedHuman": _int(confirmation_counts.get("locked_human")),
                    "confirmed": _int(confirmation_counts.get("total")),
                    "pendingReview": _int(review_counts.get("pending")),
                    "structuralIssues": _int(issue_counts.get("high")),
                    "autofix": _int(latest_score.get("needs_autofix_count")),
                },
                "pipelineStatus": pipeline_status,
                "funnelData": [
                    {"step": "Source", "value": _int(segment_counts.get("active_segments"))},
                    {"step": "Output", "value": _int(output_counts.get("with_output"))},
                    {"step": "Analisado", "value": _int(_one(con, "SELECT COUNT(DISTINCT segment_id) AS total FROM segment_analysis").get("total"))},
                    {"step": "Scored ML", "value": scored},
                    {"step": "Confirmado", "value": _int(confirmation_counts.get("total"))},
                    {"step": "Locked", "value": _int(confirmation_counts.get("locked"))},
                ],
                "packageBacklog": package_backlog,
                "humanReviews": human_reviews,
            },
            "governance": {
                "kpis": {
                    "lockedHuman": _int(confirmation_counts.get("locked_human")),
                    "blockedStructure": _int(latest_score.get("blocked_structure_count")),
                    "tokenIssues": _int(_one(con, "SELECT COUNT(*) AS total FROM issues WHERE issue_type LIKE '%token%' OR issue_type LIKE '%placeholder%'").get("total")),
                    "falseSafeHoldout": _int(active_model.get("false_safe_count")),
                    "totalModels": model_total_count,
                    "rejectedModels": rejected_count,
                    "lastPromotion": _short_model_name(active_model.get("active_model_version")),
                },
                "promotionTimeline": promotion_timeline,
                "blockReasons": block_reasons,
                "confirmationSources": confirmation_sources,
                "policy": [
                    {"title": "Threshold seguro atual", "value": f"{round(_num(active_model.get('safe_threshold')) * 100, 1)}%"},
                    {"title": "Promocao de modelo", "value": "False safe precisa ser 0"},
                    {"title": "Locked humano", "value": "Nunca sobrescrever automaticamente"},
                    {"title": "Estrutura e tokens", "value": "Prioridade sobre ML"},
                ],
                "modelSnapshot": {
                    "active": {
                        "label": "Modelo ativo/promovido",
                        "runId": active_model.get("active_model_run_id"),
                        "version": active_model.get("active_model_version"),
                        "accuracy": round(_num(active_model.get("accuracy")), 4),
                        "macroF1": round(_num(active_model.get("macro_f1")), 4),
                        "safePrecision": round(active_precision, 4),
                        "safeRecall": round(_num(active_model.get("safe_recall")), 4),
                        "holdoutCoverage": round(_num(active_model.get("safe_recall")), 4),
                        "negativeCoverage": _dataset_negative_coverage(con, active_model.get("dataset_run_id")),
                    },
                    "latest": {
                        "label": "Último modelo treinado",
                        "runId": latest_model.get("id"),
                        "version": latest_model.get("model_version"),
                        "accuracy": round(_num(latest_model.get("accuracy")), 4),
                        "macroF1": round(_num(latest_model.get("macro_f1")), 4),
                        "safePrecision": round(latest_precision, 4),
                        "safeRecall": round(_num(latest_model.get("safe_recall")), 4),
                        "holdoutCoverage": round(_num(latest_model.get("safe_recall")), 4),
                        "negativeCoverage": _dataset_negative_coverage(con, latest_model.get("dataset_run_id")),
                    },
                },
            },
            "policy": _policy_payload(con),
            "lab": _lab_payload(con),
            "specialists": _specialists_payload(con),
        }
    finally:
        con.close()


class DashboardHandler(BaseHTTPRequestHandler):
    db_path: Path = DEFAULT_DB

    def _send_html(self, status: int, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        self._send_json(204, {})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("", "/", "/api"):
            self._send_html(
                200,
                """
                <!doctype html>
                <html lang="pt-BR">
                  <head>
                    <meta charset="utf-8" />
                    <title>CK3 PT-BR Dashboard API</title>
                    <style>
                      body { font-family: system-ui, sans-serif; margin: 40px; line-height: 1.5; }
                      code { background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }
                    </style>
                  </head>
                  <body>
                    <h1>CK3 PT-BR Dashboard API</h1>
                    <p>Este servidor é apenas o backend de dados.</p>
                    <p>Abra o dashboard visual em <code>http://127.0.0.1:5173</code> depois de iniciar o frontend.</p>
                    <p>Endpoint JSON: <a href="/api/dashboard">/api/dashboard</a></p>
                    <p>Health check: <a href="/api/health">/api/health</a></p>
                  </body>
                </html>
                """,
            )
            return
        if path == "/api/health":
            self._send_json(200, {"ok": self.db_path.exists(), "databasePath": str(self.db_path)})
            return
        if path == "/api/dashboard":
            if not self.db_path.exists():
                self._send_json(500, {"error": f"SQLite not found: {self.db_path}"})
                return
            try:
                self._send_json(200, _dashboard_payload(self.db_path))
            except Exception as exc:  # pragma: no cover - server diagnostic path
                self._send_json(500, {"error": str(exc)})
            return
        self._send_json(404, {"error": "Not found"})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[dashboard] {self.address_string()} - {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="CK3 PT-BR dashboard read-only API.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to translation_engine.sqlite")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    DashboardHandler.db_path = Path(args.db).resolve()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard API: http://{args.host}:{args.port}/api/dashboard")
    print(f"SQLite: {DashboardHandler.db_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
