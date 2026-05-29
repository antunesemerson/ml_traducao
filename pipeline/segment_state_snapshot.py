from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

import db
from apply_safe_output_updates import escape_localization_value


RULE_VERSION = "segment_state_snapshot_v1"

HUMAN_LEVELS = {"human", "manual", "reviewed", "confirmed"}
HUMAN_SOURCE_HINTS = {"human", "manual", "review", "codex", "gemini"}
STRUCTURAL_ACTIONS = {"blocked_structure"}
REPAIR_ACTIONS = {"needs_autofix"}
HUMAN_REVIEW_ACTIONS = {"needs_human"}
SAFE_ACTIONS = {"auto_safe"}


@dataclass(frozen=True)
class SegmentState:
    final_state: str
    state_group: str
    output_state: str
    review_state: str
    apply_state: str
    has_output: bool
    source_blank: bool
    confirmed_matches_output: bool
    needs_human: bool
    needs_output_apply: bool
    needs_reopen: bool
    is_closed: bool
    priority_score: float
    reasons: list[str]


def is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def canonical_localization_text(value: Any) -> str:
    return escape_localization_value(as_text(value))


def latest_active_score_run(conn) -> int | None:
    row = conn.execute(
        """
        SELECT r.id
        FROM ml_model_registry registry
        JOIN ml_score_runs r ON r.model_run_id = registry.active_model_run_id
        WHERE registry.model_kind = 'risk_action_classifier'
          AND r.path_filter IS NULL
          AND r.limit_count IS NULL
          AND r.finished_at IS NOT NULL
          AND r.scored_count > 0
        ORDER BY r.finished_at DESC, r.id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def latest_candidate_score_run(conn) -> int | None:
    row = conn.execute(
        """
        SELECT r.id
        FROM ml_score_runs r
        JOIN ml_model_runs m ON m.id = r.model_run_id
        WHERE m.model_kind = 'risk_action_classifier'
          AND r.path_filter IS NULL
          AND r.limit_count IS NULL
          AND r.finished_at IS NOT NULL
          AND r.scored_count > 0
        ORDER BY r.finished_at DESC, r.id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def latest_policy_run(conn) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM ml_policy_runs
        WHERE finished_at IS NOT NULL
          AND scored_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def latest_agent_routing_run(conn) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM ml_agent_routing_runs
        WHERE finished_at IS NOT NULL
          AND routed_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def insert_run(
    conn,
    *,
    active_score_run_id: int | None,
    candidate_score_run_id: int | None,
    policy_run_id: int | None,
    agent_routing_run_id: int | None,
    started_at: str,
) -> int:
    notes = {
        "purpose": "materialized operational lifecycle state per active localization segment",
        "agent_routing_run_id": agent_routing_run_id,
        "safe_behavior": "does not modify output, confirmations, suggestions, or ML scores",
    }
    cur = conn.execute(
        """
        INSERT INTO segment_state_runs (
            rule_version,
            active_score_run_id,
            candidate_score_run_id,
            policy_run_id,
            notes_json,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            active_score_run_id,
            candidate_score_run_id,
            policy_run_id,
            json.dumps(notes, ensure_ascii=False, sort_keys=True),
            started_at,
            started_at,
        ),
    )
    return int(cur.lastrowid)


def confirmation_review_state(row) -> str:
    if row["confirmation_level"] is None:
        return "unreviewed"
    if int(row["locked"] or 0):
        return "human_locked"
    level = as_text(row["confirmation_level"]).lower()
    source = as_text(row["confirmation_source"]).lower()
    if level in HUMAN_LEVELS or any(hint in source for hint in HUMAN_SOURCE_HINTS):
        return "human_confirmed"
    return "auto_confirmed"


def current_action_columns(row) -> list[str]:
    columns = ["policy_action", "candidate_action"]
    if row["policy_action"] is None and row["candidate_action"] is None:
        columns.append("active_action")
    return columns


def risky_action(row) -> str | None:
    for column in current_action_columns(row):
        action = row[column]
        if action in STRUCTURAL_ACTIONS:
            return "blocked_structure"
    for column in current_action_columns(row):
        action = row[column]
        if action in REPAIR_ACTIONS:
            return "needs_autofix"
    return None


def needs_human_action(row) -> bool:
    return any(row[column] in HUMAN_REVIEW_ACTIONS for column in current_action_columns(row))


def safe_action(row) -> bool:
    return any(row[column] in SAFE_ACTIONS for column in current_action_columns(row))


def classify(row) -> SegmentState:
    spanish_blank = is_blank(row["spanish_text"])
    english_blank = is_blank(row["english_text"])
    old_blank = is_blank(row["old_text"])
    source_blank = spanish_blank and english_blank and old_blank
    output_blank = is_blank(row["portuguese_text"])
    has_output = not output_blank
    high_issue_count = int(row["high_issue_count"] or 0)
    issue_count = int(row["issue_count"] or 0)
    has_confirmation = row["confirmation_level"] is not None
    confirmation_blank = has_confirmation and is_blank(row["confirmed_text"])
    confirmed_matches_output = False
    if has_confirmation:
        confirmed_canonical = canonical_localization_text(row["confirmed_text"])
        output_canonical = canonical_localization_text(row["portuguese_text"])
        confirmed_matches_output = (
            confirmation_blank and output_blank
        ) or (
            not confirmation_blank and row["portuguese_text"] is not None and confirmed_canonical == output_canonical
        )

    review_state = confirmation_review_state(row)
    danger = risky_action(row)
    reasons: list[str] = []

    def closed_state(final_state: str, output_state: str, extra_reason: str) -> SegmentState:
        reasons.append(extra_reason)
        watch = bool(
            danger
            or high_issue_count > 0
            or (
                row["candidate_action"] is not None
                and row["active_action"] is not None
                and row["candidate_action"] != row["active_action"]
            )
        )
        final = f"{final_state}_watch" if watch and final_state.startswith("closed_auto") else final_state
        if watch and final != final_state:
            reasons.append("closed but kept in low-priority watch because model/guard signals disagree")
        return SegmentState(
            final_state=final,
            state_group="closed",
            output_state=output_state,
            review_state=review_state,
            apply_state="applied",
            has_output=has_output,
            source_blank=source_blank,
            confirmed_matches_output=confirmed_matches_output,
            needs_human=False,
            needs_output_apply=False,
            needs_reopen=False,
            is_closed=True,
            priority_score=5.0 if watch else 0.0,
            reasons=reasons.copy(),
        )

    if has_confirmation and not confirmed_matches_output:
        if not confirmation_blank:
            reasons.extend(["confirmed text exists but output differs or is blank", "safe output apply candidate"])
            return SegmentState(
                final_state="pending_apply_confirmed",
                state_group="pending",
                output_state="confirmation_mismatch" if has_output else "output_missing",
                review_state=review_state,
                apply_state="needs_apply",
                has_output=has_output,
                source_blank=source_blank,
                confirmed_matches_output=False,
                needs_human=False,
                needs_output_apply=True,
                needs_reopen=False,
                is_closed=False,
                priority_score=100.0,
                reasons=reasons,
            )
        reasons.extend(["confirmed blank does not match nonblank output", "intentional blank needs output apply"])
        return SegmentState(
            final_state="pending_apply_intentional_blank",
            state_group="pending",
            output_state="confirmation_mismatch",
            review_state=review_state,
            apply_state="needs_apply",
            has_output=has_output,
            source_blank=source_blank,
            confirmed_matches_output=False,
            needs_human=False,
            needs_output_apply=True,
            needs_reopen=False,
            is_closed=False,
            priority_score=95.0,
            reasons=reasons,
        )

    if source_blank and output_blank:
        review = review_state if has_confirmation else "source_blank"
        reasons.append("source and output are blank; valid structural blank")
        return SegmentState(
            final_state="closed_valid_blank",
            state_group="closed",
            output_state="blank_valid",
            review_state=review,
            apply_state="no_action",
            has_output=False,
            source_blank=True,
            confirmed_matches_output=confirmed_matches_output,
            needs_human=False,
            needs_output_apply=False,
            needs_reopen=False,
            is_closed=True,
            priority_score=0.0,
            reasons=reasons,
        )

    if has_confirmation and confirmation_blank and output_blank:
        reasons.append("human/learning confirmation intentionally keeps output blank")
        return SegmentState(
            final_state="closed_intentional_blank",
            state_group="closed",
            output_state="blank_intentional",
            review_state=review_state,
            apply_state="applied",
            has_output=False,
            source_blank=source_blank,
            confirmed_matches_output=True,
            needs_human=False,
            needs_output_apply=False,
            needs_reopen=False,
            is_closed=True,
            priority_score=0.0,
            reasons=reasons,
        )

    if has_confirmation and confirmed_matches_output:
        if review_state in {"human_locked", "human_confirmed"}:
            if danger or high_issue_count > 0:
                reasons.append("manual confirmation is preserved despite model/issue watch signal")
            return closed_state(
                "closed_human_confirmed" if review_state == "human_confirmed" else "closed_human_locked",
                "output_present",
                "confirmed output matches trusted human text",
            )
        if danger == "blocked_structure" or high_issue_count > 0:
            reasons.extend(["auto-confirmed output has structural/high issue signal", "requires reopening before final closure"])
            return SegmentState(
                final_state="reopen_auto_confirmed",
                state_group="pending",
                output_state="output_present",
                review_state=review_state,
                apply_state="needs_review",
                has_output=True,
                source_blank=source_blank,
                confirmed_matches_output=True,
                needs_human=True,
                needs_output_apply=False,
                needs_reopen=True,
                is_closed=False,
                priority_score=90.0 + min(issue_count, 10),
                reasons=reasons,
            )
        if danger == "needs_autofix":
            reasons.extend(["auto-confirmed output has autofix signal", "requires targeted repair review"])
            return SegmentState(
                final_state="reopen_auto_confirmed_autofix",
                state_group="pending",
                output_state="output_present",
                review_state=review_state,
                apply_state="needs_review",
                has_output=True,
                source_blank=source_blank,
                confirmed_matches_output=True,
                needs_human=True,
                needs_output_apply=False,
                needs_reopen=True,
                is_closed=False,
                priority_score=70.0 + min(issue_count, 10),
                reasons=reasons,
            )
        return closed_state("closed_auto_confirmed", "output_present", "confirmed output matches learned/auto text")

    if not has_output:
        reasons.append("source has content but output is blank and no valid blank confirmation was found")
        return SegmentState(
            final_state="pending_output_missing_real",
            state_group="pending",
            output_state="output_missing",
            review_state=review_state,
            apply_state="needs_review",
            has_output=False,
            source_blank=source_blank,
            confirmed_matches_output=False,
            needs_human=True,
            needs_output_apply=False,
            needs_reopen=False,
            is_closed=False,
            priority_score=85.0,
            reasons=reasons,
        )

    if danger == "blocked_structure" or high_issue_count > 0:
        reasons.append("output exists but structural/high issue signal blocks closure")
        return SegmentState(
            final_state="pending_blocked_structure",
            state_group="pending",
            output_state="output_present",
            review_state=review_state,
            apply_state="needs_review",
            has_output=True,
            source_blank=source_blank,
            confirmed_matches_output=False,
            needs_human=True,
            needs_output_apply=False,
            needs_reopen=False,
            is_closed=False,
            priority_score=80.0 + min(issue_count, 10),
            reasons=reasons,
        )

    if danger == "needs_autofix":
        reasons.append("output exists but current model recommends autofix")
        return SegmentState(
            final_state="pending_autofix",
            state_group="pending",
            output_state="output_present",
            review_state=review_state,
            apply_state="needs_review",
            has_output=True,
            source_blank=source_blank,
            confirmed_matches_output=False,
            needs_human=True,
            needs_output_apply=False,
            needs_reopen=False,
            is_closed=False,
            priority_score=65.0,
            reasons=reasons,
        )

    if safe_action(row):
        reasons.append("model/policy marks as safe but no confirmation row exists")
        return SegmentState(
            final_state="ready_auto_confirm",
            state_group="pending",
            output_state="output_present",
            review_state=review_state,
            apply_state="needs_review",
            has_output=True,
            source_blank=source_blank,
            confirmed_matches_output=False,
            needs_human=False,
            needs_output_apply=False,
            needs_reopen=False,
            is_closed=False,
            priority_score=25.0,
            reasons=reasons,
        )

    if needs_human_action(row):
        reasons.append("current model/policy asks for human review")
        return SegmentState(
            final_state="pending_human_review",
            state_group="pending",
            output_state="output_present",
            review_state=review_state,
            apply_state="needs_review",
            has_output=True,
            source_blank=source_blank,
            confirmed_matches_output=False,
            needs_human=True,
            needs_output_apply=False,
            needs_reopen=False,
            is_closed=False,
            priority_score=50.0,
            reasons=reasons,
        )

    reasons.append("no closure signal was strong enough")
    return SegmentState(
        final_state="pending_unknown_open",
        state_group="pending",
        output_state="output_present",
        review_state=review_state,
        apply_state="needs_review",
        has_output=True,
        source_blank=source_blank,
        confirmed_matches_output=False,
        needs_human=True,
        needs_output_apply=False,
        needs_reopen=False,
        is_closed=False,
        priority_score=40.0,
        reasons=reasons,
    )


def iter_segments(conn, active_score_run_id: int | None, candidate_score_run_id: int | None, policy_run_id: int | None, limit: int | None):
    sql = """
        WITH issue_summary AS (
            SELECT
                segment_id,
                COUNT(*) AS issue_count,
                SUM(CASE WHEN lower(severity) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count,
                GROUP_CONCAT(DISTINCT issue_type) AS issue_types
            FROM issues
            GROUP BY segment_id
        )
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.spanish_text,
            s.english_text,
            s.old_text,
            o.portuguese_text,
            sc.confirmation_level,
            sc.confirmation_source,
            sc.confirmation_label,
            sc.confirmed_text,
            COALESCE(sc.locked, 0) AS locked,
            active.final_action AS active_action,
            candidate.final_action AS candidate_action,
            policy.policy_action AS policy_action,
            COALESCE(issue_summary.issue_count, 0) AS issue_count,
            COALESCE(issue_summary.high_issue_count, 0) AS high_issue_count,
            issue_summary.issue_types
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        LEFT JOIN ml_score_items active ON active.segment_id = s.id AND active.run_id = ?
        LEFT JOIN ml_score_items candidate ON candidate.segment_id = s.id AND candidate.run_id = ?
        LEFT JOIN ml_policy_items policy ON policy.segment_id = s.id AND policy.run_id = ?
        LEFT JOIN issue_summary ON issue_summary.segment_id = s.id
        WHERE s.is_active = 1
        ORDER BY s.relative_path, s.source_line_number, s.id
    """
    if limit:
        sql += "\n        LIMIT ?"
        params = (active_score_run_id, candidate_score_run_id, policy_run_id, limit)
    else:
        params = (active_score_run_id, candidate_score_run_id, policy_run_id)
    return conn.execute(sql, params)


def insert_items(conn, rows: list[tuple[Any, ...]]) -> None:
    conn.executemany(
        """
        INSERT INTO segment_state_items (
            run_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            final_state,
            state_group,
            output_state,
            review_state,
            apply_state,
            active_action,
            candidate_action,
            policy_action,
            confirmation_level,
            confirmation_label,
            locked,
            has_output,
            source_blank,
            confirmed_matches_output,
            needs_human,
            needs_output_apply,
            needs_reopen,
            is_closed,
            priority_score,
            reasons_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def update_run_summary(conn, run_id: int, finished_at: str, counters: dict[str, Counter]) -> None:
    total = sum(counters["state_group"].values())
    closed = counters["state_group"].get("closed", 0)
    pending = total - closed
    conn.execute(
        """
        UPDATE segment_state_runs
        SET
            total_segments = ?,
            closed_count = ?,
            pending_count = ?,
            output_apply_pending_count = ?,
            blank_valid_count = ?,
            experimental_watch_count = ?,
            reopen_count = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            total,
            closed,
            pending,
            counters["apply_state"].get("needs_apply", 0),
            counters["final_state"].get("closed_valid_blank", 0) + counters["final_state"].get("closed_intentional_blank", 0),
            sum(count for state, count in counters["final_state"].items() if state.endswith("_watch")),
            counters["needs_reopen"].get("1", 0),
            finished_at,
            finished_at,
            run_id,
        ),
    )


def top_rows(conn, run_id: int, where_sql: str, limit: int = 12) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            relative_path,
            source_line_number,
            source_key,
            final_state,
            review_state,
            active_action,
            candidate_action,
            policy_action,
            priority_score
        FROM segment_state_items
        WHERE run_id = ?
          AND {where_sql}
        ORDER BY priority_score DESC, relative_path, source_line_number
        LIMIT ?
        """,
        (run_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def format_counter(title: str, counter: Counter, total: int) -> list[str]:
    lines = [f"\n{title}:"]
    for key, value in counter.most_common():
        pct = (value / total) if total else 0
        lines.append(f"- {key}: {value:,} ({pct:.2%})")
    return lines


def build_report(conn, run_id: int, counters: dict[str, Counter], settings: dict, started_at: str, finished_at: str) -> list[str]:
    total = sum(counters["state_group"].values())
    closed = counters["state_group"].get("closed", 0)
    pending = total - closed
    package_rows = conn.execute(
        """
        SELECT
            substr(relative_path, 1, instr(relative_path || '/', '/') - 1) AS package_name,
            COUNT(*) AS total,
            SUM(CASE WHEN state_group = 'pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN needs_output_apply = 1 THEN 1 ELSE 0 END) AS needs_apply,
            SUM(CASE WHEN needs_reopen = 1 THEN 1 ELSE 0 END) AS needs_reopen
        FROM segment_state_items
        WHERE run_id = ?
        GROUP BY package_name
        HAVING pending > 0 OR needs_apply > 0 OR needs_reopen > 0
        ORDER BY pending DESC, needs_apply DESC, package_name
        LIMIT 15
        """,
        (run_id,),
    ).fetchall()
    lines = [
        "Segment State Snapshot",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Started at: {started_at}",
        f"Finished at: {finished_at}",
        "",
        "Resumo:",
        f"- Segmentos ativos analisados: {total:,}",
        f"- Fechados/consolidados: {closed:,} ({(closed / total if total else 0):.2%})",
        f"- Pendentes operacionais: {pending:,} ({(pending / total if total else 0):.2%})",
        f"- Precisam aplicar output confirmado: {counters['apply_state'].get('needs_apply', 0):,}",
        f"- Blanks válidos/intencionais: {counters['final_state'].get('closed_valid_blank', 0) + counters['final_state'].get('closed_intentional_blank', 0):,}",
        f"- Reabertura recomendada: {counters['needs_reopen'].get('1', 0):,}",
    ]
    lines.extend(format_counter("Por grupo", counters["state_group"], total))
    lines.extend(format_counter("Por estado final", counters["final_state"], total))
    lines.extend(format_counter("Por estado de output", counters["output_state"], total))
    lines.extend(format_counter("Por estado de revisão", counters["review_state"], total))
    lines.extend(format_counter("Por estado de aplicação", counters["apply_state"], total))

    if package_rows:
        lines.append("\nPacotes com mais pendência:")
        for row in package_rows:
            lines.append(
                "- "
                f"{row['package_name']}: pending={row['pending']:,}, "
                f"needs_apply={row['needs_apply']:,}, reopen={row['needs_reopen']:,}, total={row['total']:,}"
            )

    apply_rows = top_rows(conn, run_id, "needs_output_apply = 1", limit=15)
    if apply_rows:
        lines.append("\nTop candidatos para aplicar output confirmado:")
        for row in apply_rows:
            lines.append(
                "- "
                f"segment={row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | "
                f"{row['source_key']} | {row['final_state']} | review={row['review_state']}"
            )

    reopen_rows = top_rows(conn, run_id, "needs_reopen = 1", limit=15)
    if reopen_rows:
        lines.append("\nTop candidatos para reabrir/revisar:")
        for row in reopen_rows:
            lines.append(
                "- "
                f"segment={row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | "
                f"{row['source_key']} | {row['final_state']} | "
                f"active={row['active_action']} candidate={row['candidate_action']} policy={row['policy_action']}"
            )

    report_path = db.write_report(settings, "segment_state_snapshot", lines)
    lines.append("")
    lines.append(f"Report: {report_path}")
    return lines


def main(limit: int | None = None) -> int:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        active_score_run_id = latest_active_score_run(conn)
        candidate_score_run_id = latest_candidate_score_run(conn)
        policy_run_id = latest_policy_run(conn)
        agent_routing_run_id = latest_agent_routing_run(conn)
        run_id = insert_run(
            conn,
            active_score_run_id=active_score_run_id,
            candidate_score_run_id=candidate_score_run_id,
            policy_run_id=policy_run_id,
            agent_routing_run_id=agent_routing_run_id,
            started_at=started_at,
        )
        conn.commit()

        counters: dict[str, Counter] = defaultdict(Counter)
        batch: list[tuple[Any, ...]] = []
        created_at = started_at
        for row in iter_segments(conn, active_score_run_id, candidate_score_run_id, policy_run_id, limit):
            state = classify(row)
            counters["final_state"][state.final_state] += 1
            counters["state_group"][state.state_group] += 1
            counters["output_state"][state.output_state] += 1
            counters["review_state"][state.review_state] += 1
            counters["apply_state"][state.apply_state] += 1
            counters["needs_reopen"]["1" if state.needs_reopen else "0"] += 1
            batch.append(
                (
                    run_id,
                    int(row["segment_id"]),
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    state.final_state,
                    state.state_group,
                    state.output_state,
                    state.review_state,
                    state.apply_state,
                    row["active_action"],
                    row["candidate_action"],
                    row["policy_action"],
                    row["confirmation_level"],
                    row["confirmation_label"],
                    int(row["locked"] or 0),
                    1 if state.has_output else 0,
                    1 if state.source_blank else 0,
                    1 if state.confirmed_matches_output else 0,
                    1 if state.needs_human else 0,
                    1 if state.needs_output_apply else 0,
                    1 if state.needs_reopen else 0,
                    1 if state.is_closed else 0,
                    state.priority_score,
                    json.dumps(state.reasons, ensure_ascii=False),
                    created_at,
                )
            )
            if len(batch) >= 5000:
                insert_items(conn, batch)
                conn.commit()
                batch.clear()
        if batch:
            insert_items(conn, batch)
            conn.commit()

        finished_at = db.utc_now()
        update_run_summary(conn, run_id, finished_at, counters)
        conn.commit()
        lines = build_report(conn, run_id, counters, settings, started_at, finished_at)

    for line in lines:
        print(line)
    return run_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a final lifecycle state snapshot for active segments.")
    parser.add_argument("--limit", type=int, default=None, help="Optional debug limit.")
    args = parser.parse_args()
    main(limit=args.limit)
