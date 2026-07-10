from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import general_pipe_article_gender_lifecycle_policy_materializer as bridge


RULE_VERSION = "human_confirmed_local_learning_lifecycle_policy_materializer_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_paths(mode: str) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_human_confirmed_local_learning_lifecycle_policy_materializer_{mode}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".jsonl"),
        reports_dir() / f"{base.name}_summary.json",
        base.with_suffix(".csv"),
    )


def parse_segment_ids(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    segment_ids = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    if not segment_ids:
        raise SystemExit("empty --segment-ids")
    return segment_ids


def latest_segment_state_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) AS id FROM segment_state_runs WHERE finished_at IS NOT NULL").fetchone()
    if not row or row["id"] is None:
        raise SystemExit("missing finished segment-state run")
    return int(row["id"])


def existing_active_policy_item_clause(include_existing: bool) -> str:
    if include_existing:
        return ""
    return """
      AND NOT EXISTS (
          SELECT 1
          FROM auto_confirmation_reopen_lifecycle_policy_items item
          JOIN auto_confirmation_reopen_lifecycle_policy_runs policy_run
            ON policy_run.id = item.run_id
          WHERE item.segment_id = c.segment_id
            AND policy_run.policy_name = ?
            AND policy_run.policy_status = 'active'
            AND item.policy_allowed = 1
            AND item.policy_action = ?
      )
    """


def discover_segment_ids(
    conn: sqlite3.Connection,
    *,
    learning_run_id: int,
    segment_state_run_id: int,
    include_existing: bool,
) -> tuple[int, ...]:
    labels = tuple(sorted(bridge.ALLOWED_CONFIRMATION_LABELS))
    label_placeholders = ",".join("?" for _ in labels)
    label_predicates = []
    params_labels: list[Any] = []
    if labels:
        label_predicates.append(f"c.confirmation_label IN ({label_placeholders})")
        params_labels.extend(labels)
    for prefix in getattr(bridge, "ALLOWED_CONFIRMATION_LABEL_PREFIXES", ()):
        for suffix in getattr(bridge, "ALLOWED_CONFIRMATION_LABEL_SUFFIXES", ()):
            label_predicates.append("(c.confirmation_label LIKE ? AND c.confirmation_label LIKE ?)")
            params_labels.extend([f"{prefix}%", f"%{suffix}"])
    if not label_predicates:
        raise SystemExit("missing allowed confirmation label predicates")
    label_clause = " OR ".join(label_predicates)
    query = f"""
        SELECT DISTINCT c.segment_id
        FROM local_learning_candidates ll
        JOIN segment_confirmations c
          ON c.candidate_id = ll.id
        JOIN segment_state_items state
          ON state.segment_id = c.segment_id
         AND state.run_id = ?
        JOIN output_segments output
          ON output.segment_id = c.segment_id
        WHERE ll.run_id = ?
          AND state.state_group = 'pending'
          AND state.final_state = 'reopen_auto_confirmed_autofix'
          AND COALESCE(state.needs_output_apply, 0) = 0
          AND COALESCE(state.confirmed_matches_output, 0) = 1
          AND c.confirmation_level = 'human_confirmed'
          AND c.confirmation_source IN ({",".join("?" for _ in sorted(bridge.ALLOWED_CONFIRMATION_SOURCES))})
          AND ({label_clause})
          AND COALESCE(c.locked, 0) = 1
          AND output.portuguese_text = c.confirmed_text
          {existing_active_policy_item_clause(include_existing)}
        ORDER BY c.segment_id
    """
    params: list[Any] = [
        segment_state_run_id,
        learning_run_id,
        *sorted(bridge.ALLOWED_CONFIRMATION_SOURCES),
        *params_labels,
    ]
    if not include_existing:
        params.extend([bridge.POLICY_NAME, bridge.POLICY_ACTION])
    return tuple(int(row["segment_id"]) for row in conn.execute(query, params).fetchall())


def configure_bridge(segment_ids: tuple[int, ...], state_run_id: int) -> None:
    bridge.RULE_VERSION = RULE_VERSION
    bridge.TARGET_SEGMENT_IDS = segment_ids
    bridge.DEFAULT_SEGMENT_STATE_RUN_ID = state_run_id


def write_empty_reports(
    *,
    mode: str,
    learning_run_id: int,
    segment_state_run_id: int,
    ledger_run_id: int,
    txt_path: Path,
    jsonl_path: Path,
    summary_path: Path,
    csv_path: Path,
) -> dict[str, Any]:
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "learning_run_id": learning_run_id,
        "segment_state_run_id": segment_state_run_id,
        "ledger_run_id": ledger_run_id,
        "policy_name": bridge.POLICY_NAME,
        "policy_action": bridge.POLICY_ACTION,
        "policy_run_id": None,
        "candidate_count": 0,
        "released_count": 0,
        "blocked_count": 0,
        "target_segment_ids": [],
        "expected_delta_after_segment_state": {
            "closed_count": 0,
            "pending_count": 0,
            "reopen_count": 0,
            "output_apply_pending_count": 0,
        },
        "run_segment_state_now": False,
        "run_reindex_now": False,
        "run_lifecycle_now": False,
        "run_production_full_now": False,
        "writes_source": False,
        "writes_output": False,
        "rows": [],
    }
    txt_path.write_text(
        "\n".join(
            [
                "Human-confirmed local-learning lifecycle policy materializer",
                f"mode={mode}",
                f"learning_run_id={learning_run_id}",
                f"segment_state_run_id={segment_state_run_id}",
                "candidate_count=0",
                "released_count=0",
                "blocked_count=0",
                "No eligible new policy items found.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    jsonl_path.write_text("", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    csv_path.write_text("segment_id,policy_allowed,block_reason,relative_path,source_key\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--learning-run-id", type=int, required=True)
    parser.add_argument("--segment-state-run-id", type=int)
    parser.add_argument("--ledger-run-id", type=int)
    parser.add_argument("--segment-ids", help="Optional comma-separated override; otherwise selects eligible rows from learning run.")
    parser.add_argument("--include-existing", action="store_true", help="Audit existing active policy items instead of excluding them.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    mode = "apply" if args.apply else "dry_run"
    txt_path, jsonl_path, summary_path, csv_path = output_paths(mode)

    if args.apply:
        settings = db.load_settings()
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            conn.row_factory = sqlite3.Row
            segment_state_run_id = args.segment_state_run_id or latest_segment_state_run_id(conn)
            ledger_run_id = args.ledger_run_id or bridge.latest_finished_ledger_run_id(conn)
            segment_ids = parse_segment_ids(args.segment_ids) or discover_segment_ids(
                conn,
                learning_run_id=args.learning_run_id,
                segment_state_run_id=segment_state_run_id,
                include_existing=args.include_existing,
            )
            if not segment_ids:
                summary = write_empty_reports(
                    mode=mode,
                    learning_run_id=args.learning_run_id,
                    segment_state_run_id=segment_state_run_id,
                    ledger_run_id=ledger_run_id,
                    txt_path=txt_path,
                    jsonl_path=jsonl_path,
                    summary_path=summary_path,
                    csv_path=csv_path,
                )
            else:
                configure_bridge(segment_ids, segment_state_run_id)
                rows = bridge.collect_rows(conn, segment_state_run_id, ledger_run_id)
                if sum(1 for row in rows if row["policy_allowed"]) != len(segment_ids):
                    bridge.write_reports(rows, mode, segment_state_run_id, ledger_run_id, None, txt_path, jsonl_path, summary_path, csv_path)
                    raise SystemExit("apply blocked: not all target segments are policy_allowed")
                policy_run_id = bridge.apply_policy_run(conn, rows, txt_path, csv_path, jsonl_path, segment_state_run_id, ledger_run_id)
                summary = bridge.write_reports(rows, mode, segment_state_run_id, ledger_run_id, policy_run_id, txt_path, jsonl_path, summary_path, csv_path)
                summary["learning_run_id"] = args.learning_run_id
                summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            conn.commit()
    else:
        with bridge.readonly_conn() as conn:
            segment_state_run_id = args.segment_state_run_id or latest_segment_state_run_id(conn)
            ledger_run_id = args.ledger_run_id or bridge.latest_finished_ledger_run_id(conn)
            segment_ids = parse_segment_ids(args.segment_ids) or discover_segment_ids(
                conn,
                learning_run_id=args.learning_run_id,
                segment_state_run_id=segment_state_run_id,
                include_existing=args.include_existing,
            )
            if not segment_ids:
                summary = write_empty_reports(
                    mode=mode,
                    learning_run_id=args.learning_run_id,
                    segment_state_run_id=segment_state_run_id,
                    ledger_run_id=ledger_run_id,
                    txt_path=txt_path,
                    jsonl_path=jsonl_path,
                    summary_path=summary_path,
                    csv_path=csv_path,
                )
            else:
                configure_bridge(segment_ids, segment_state_run_id)
                rows = bridge.collect_rows(conn, segment_state_run_id, ledger_run_id)
                summary = bridge.write_reports(rows, mode, segment_state_run_id, ledger_run_id, None, txt_path, jsonl_path, summary_path, csv_path)
                summary["learning_run_id"] = args.learning_run_id
                summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"csv={csv_path}")
    print(f"mode={mode}")
    print(f"learning_run_id={args.learning_run_id}")
    print(f"policy_run_id={summary['policy_run_id'] or ''}")
    print(f"candidate_count={summary['candidate_count']}")
    print(f"released_count={summary['released_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print("writes_source=false")
    print("writes_output=false")
    print("runs_lifecycle=false")
    print("runs_segment_state=false")


if __name__ == "__main__":
    main()
