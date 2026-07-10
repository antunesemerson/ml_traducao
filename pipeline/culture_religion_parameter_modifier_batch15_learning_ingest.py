from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "culture_religion_parameter_modifier_batch15_learning_ingest_v1"
QUEUE_SOURCE = "culture-religion-parameter-modifier-human-review-batch15"
ORIGIN = "human_confirmed_culture_religion_parameter_modifier_batch15"
MATCH_TYPE_ALREADY_OK = "culture_religion_parameter_modifier_human_confirmed_already_ok"
MATCH_TYPE_CORRECTION = "culture_religion_parameter_modifier_human_correction"
REVIEWER = "user_human_review_culture_religion_parameter_modifier_batch15"
FOCUS_GROUP = "culture_religion_parameter_modifier_batch15"

APPROVED_ALREADY_OK_SEGMENT_IDS = [
    20471,
]

APPROVED_CORRECTIONS = {
    20449: "Ao retornar de um [raid|lE] bem-sucedido, os personagens não [Concept( 'tribal', 'tribais' )|E] #EMP perdem#! [EmptyScope.ScriptValue('not_tribal_raid_prestige_multiplier')|1] de [prestige_i][prestige|lE] por 1 [loot|lE] entregue",
    20451: "Ao defender seu [primary_title|lE] ou sua independência em uma [war|lE], os [rulers|lE] [Concept( 'independent', 'independentes' )|E] de [rank|lE] [duke|lE] ou inferior recebem uma bonificação de #P [EmptyScope.ScriptValue('determined_independence_defensive_advantage_value')|0]#! de [advantage|lE] em [battle|lE]",
    20452: "Os [rulers|lE] [Concept( 'independent', 'independentes' )|E] ganham [dynasty_prestige_i][dynasty_prestige|lE] ao vencer certas [wars|lE], como Guerras de Independência como atacante ou Guerras de Vassalização como defensor",
    20453: "É mais provável que os [rulers|lE] [Concept( 'independent', 'independentes' )|E] que sejam os defensores principais em uma [war|lE] não religiosa recebam pedidos de #high $join_war_interaction$#! de $game_concept_rulers$ vizinhos da mesma [culture|lE]",
    20718: "Os [characters|lE] tomam ações mais ousadas e é mais difícil deixá-los [Concept( 'intimidated', 'intimidados' )|E] ou [Concept( 'cowed', 'acovardados' )|E]",
    21234: "É mais provável que os [rulers|lE] de outra [culture|lE] [Concept( 'hybridize', 'hibridizem' )|E] com esta [culture|lE]",
    21362: "Além disso, esses [Concept('close_family_members', 'familiares próximos')|E] serão tratados como tendo sido aprovados no [GetActivityType( 'activity_local_examination' ).GetName|l] @provincial_exam_icon!",
}

HOLD_SEGMENT_IDS: dict[int, str] = {}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fetch_context(conn, segment_id: int):
    return conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id = ?
        """,
        (segment_id,),
    ).fetchone()


def fetch_snapshot(conn, segment_ids: list[int]) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in segment_ids)
    return {
        "local_learning_candidates": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM local_learning_candidates WHERE segment_id IN ({placeholders}) ORDER BY segment_id, id",
                tuple(segment_ids),
            )
        ],
        "segment_confirmations": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM segment_confirmations WHERE segment_id IN ({placeholders}) ORDER BY segment_id, id",
                tuple(segment_ids),
            )
        ],
    }


def existing_candidate(conn, segment_id: int, suggested_hash: str):
    return conn.execute(
        """
        SELECT id
        FROM local_learning_candidates
        WHERE segment_id = ?
          AND suggested_hash = ?
          AND queue_source = ?
          AND origin = ?
        ORDER BY id ASC
        LIMIT 1
        """,
        (segment_id, suggested_hash, QUEUE_SOURCE, ORIGIN),
    ).fetchone()


def token_markers(text: str) -> set[str]:
    markers = set()
    for marker in ("[", "]", "$", "#", "#!", "Concept", "EmptyScope.ScriptValue", "GetActivityType"):
        if marker in text:
            markers.add(marker)
    return markers


def token_guard(current: str, suggested: str) -> bool:
    return token_markers(current) <= token_markers(suggested)


def create_run(conn, inserted_count: int, skipped_count: int, blocked_count: int, timestamp: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO local_learning_runs (
            mode, limit_count, auto_confidence_threshold, candidate_count,
            high_confidence_count, pending_human_count, status, notes,
            started_at, finished_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            QUEUE_SOURCE,
            inserted_count + skipped_count + blocked_count + len(HOLD_SEGMENT_IDS),
            1.0,
            inserted_count,
            inserted_count,
            0,
            "completed",
            f"{RULE_VERSION}; skipped_existing={skipped_count}; blocked={blocked_count}; holds={sorted(HOLD_SEGMENT_IDS)}",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def insert_candidate(
    conn,
    run_id: int,
    context,
    suggested: str,
    human_label: str,
    match_type: str,
    corrected_text: str | None,
    timestamp: str,
) -> int:
    reasons = [
        "human_confirmed_culture_religion_parameter_modifier_batch15",
        "risk_decision:final_lane_review",
        "no_source_write",
        "no_output_write",
        "approved_in_chat",
    ]
    cursor = conn.execute(
        """
        INSERT INTO local_learning_candidates (
            run_id, feedback_id, suggestion_id, segment_id, relative_path,
            source_key, source_line_number, english_text, spanish_text, old_text,
            current_output_text, suggested_text, suggested_hash, source_language,
            origin, match_type, match_score, token_status, suggestion_status,
            local_confidence_score, local_status, human_label, corrected_text,
            reason, reviewer, reviewed_at, reasons_json, created_at, updated_at,
            queue_source, focus_group
        )
        VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            int(context["segment_id"]),
            context["relative_path"],
            context["source_key"],
            context["source_line_number"],
            context["english_text"],
            context["spanish_text"],
            context["old_text"],
            context["current_output_text"],
            suggested,
            sha256_text(suggested),
            "human_correction" if corrected_text else "human_already_ok",
            ORIGIN,
            match_type,
            1.0,
            "ok",
            "safe",
            1.0,
            "high_confidence",
            human_label,
            corrected_text,
            "human-approved culture/religion parameter modifier batch15 learning signal",
            REVIEWER,
            timestamp,
            json.dumps(reasons, ensure_ascii=True),
            timestamp,
            timestamp,
            QUEUE_SOURCE,
            FOCUS_GROUP,
        ),
    )
    return int(cursor.lastrowid)


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any], before_snapshot: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_culture_religion_parameter_modifier_batch15_learning_ingest"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    snapshot_path = reports_dir() / f"{base.name}_before_snapshot.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    snapshot_path.write_text(json.dumps(before_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "culture/religion parameter modifier batch15 learning ingest",
        f"rule_version={RULE_VERSION}",
        f"run_id={summary['run_id']}",
        f"inserted_count={summary['inserted_count']}",
        f"inserted_already_ok={summary['inserted_already_ok']}",
        f"inserted_corrections={summary['inserted_corrections']}",
        f"skipped_existing_count={summary['skipped_existing_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"snapshot_path={snapshot_path}",
        "source_changed=false",
        "output_changed=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, snapshot_path


def main() -> None:
    reviewed_ids = APPROVED_ALREADY_OK_SEGMENT_IDS + sorted(APPROVED_CORRECTIONS) + sorted(HOLD_SEGMENT_IDS)
    timestamp = now()
    records: list[dict[str, Any]] = []
    plan: list[tuple[int, Any, str, str, str, str | None]] = []
    skipped_existing_count = 0
    blocked_count = 0

    with db.connect(db.load_settings()) as conn:
        db.ensure_database(conn)
        before_snapshot = fetch_snapshot(conn, reviewed_ids)
        for segment_id in APPROVED_ALREADY_OK_SEGMENT_IDS:
            context = fetch_context(conn, segment_id)
            if not context:
                blocked_count += 1
                records.append({"segment_id": segment_id, "status": "blocked_missing_context", "decision": "already_ok"})
                continue
            current = str(context["current_output_text"] or "")
            if not current.strip():
                blocked_count += 1
                records.append({"segment_id": segment_id, "status": "blocked_blank_current_output", "decision": "already_ok"})
                continue
            existing = existing_candidate(conn, segment_id, sha256_text(current))
            if existing:
                skipped_existing_count += 1
                records.append({"segment_id": segment_id, "status": "skipped_existing", "decision": "already_ok", "existing_candidate_id": int(existing["id"])})
                continue
            plan.append((segment_id, context, current, "correct", MATCH_TYPE_ALREADY_OK, None))

        for segment_id, corrected in sorted(APPROVED_CORRECTIONS.items()):
            context = fetch_context(conn, segment_id)
            if not context:
                blocked_count += 1
                records.append({"segment_id": segment_id, "status": "blocked_missing_context", "decision": "human_correction"})
                continue
            current = str(context["current_output_text"] or "")
            if not token_guard(current, corrected):
                blocked_count += 1
                records.append({"segment_id": segment_id, "status": "blocked_token_guard", "decision": "human_correction", "corrected_text": corrected})
                continue
            existing = existing_candidate(conn, segment_id, sha256_text(corrected))
            if existing:
                skipped_existing_count += 1
                records.append({"segment_id": segment_id, "status": "skipped_existing", "decision": "human_correction", "existing_candidate_id": int(existing["id"])})
                continue
            plan.append((segment_id, context, corrected, "semantic_error", MATCH_TYPE_CORRECTION, corrected))

        run_id = create_run(conn, len(plan), skipped_existing_count, blocked_count, timestamp)
        inserted_already_ok = 0
        inserted_corrections = 0
        for segment_id, context, suggested, human_label, match_type, corrected_text in plan:
            candidate_id = insert_candidate(conn, run_id, context, suggested, human_label, match_type, corrected_text, timestamp)
            inserted_already_ok += int(corrected_text is None)
            inserted_corrections += int(corrected_text is not None)
            records.append(
                {
                    "segment_id": segment_id,
                    "candidate_id": candidate_id,
                    "run_id": run_id,
                    "status": "inserted",
                    "decision": "human_correction" if corrected_text else "already_ok",
                    "human_label": human_label,
                    "current_output_text": context["current_output_text"],
                    "suggested_text": suggested,
                    "corrected_text": corrected_text,
                }
            )

        conn.commit()

    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": timestamp,
        "run_id": run_id,
        "approved_already_ok_segment_ids": APPROVED_ALREADY_OK_SEGMENT_IDS,
        "approved_correction_segment_ids": sorted(APPROVED_CORRECTIONS),
        "hold_segment_ids": sorted(HOLD_SEGMENT_IDS),
        "inserted_count": len(plan),
        "inserted_already_ok": inserted_already_ok,
        "inserted_corrections": inserted_corrections,
        "skipped_existing_count": skipped_existing_count,
        "blocked_count": blocked_count,
        "source_changed": False,
        "output_changed": False,
        "apply_executed": False,
        "production_full_recommended_now": False,
        "next_action": "apply_local_learning_feedback_then_protected_apply_for_corrected_segments",
    }
    txt_path, jsonl_path, summary_path, snapshot_path = write_outputs(records, summary, before_snapshot)
    print(f"run_id={run_id}")
    print(f"inserted_count={summary['inserted_count']}")
    print(f"inserted_already_ok={summary['inserted_already_ok']}")
    print(f"inserted_corrections={summary['inserted_corrections']}")
    print(f"blocked_count={blocked_count}")
    print(f"skipped_existing_count={skipped_existing_count}")
    print(f"hold_count={len(HOLD_SEGMENT_IDS)}")
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"snapshot={snapshot_path}")


if __name__ == "__main__":
    main()
