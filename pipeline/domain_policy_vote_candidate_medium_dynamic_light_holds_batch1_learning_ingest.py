from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "domain_policy_vote_candidate_medium_dynamic_light_holds_batch1_learning_ingest_v1"
QUEUE_SOURCE = "domain-policy-vote-candidate-medium-dynamic-light-holds-human-packet"
ORIGIN = "human_confirmed_domain_policy_vote_candidate_medium_dynamic_light_holds_batch1"
MATCH_TYPE_ALREADY_OK = "medium_dynamic_light_holds_human_confirmed_already_ok"
MATCH_TYPE_CORRECTION = "medium_dynamic_light_holds_human_correction"
REVIEWER = "user_human_review_medium_dynamic_light_holds_batch1"
FOCUS_GROUP = "domain_policy_vote_candidate_medium_dynamic_light_holds_batch1"
PACKET_SUMMARY = Path("reports/20260629_230300_224250_domain_policy_vote_candidate_medium_dynamic_light_holds_human_packet_v1_summary.json")
PACKET_JSONL = Path("reports/20260629_230300_224250_domain_policy_vote_candidate_medium_dynamic_light_holds_human_packet_v1.jsonl")

APPROVED_ALREADY_OK_SEGMENT_IDS = [
    41941,
    238011,
    239265,
]

APPROVED_CORRECTIONS = {
    238064: "Permite a construção de megalitos em possessões [Concept( 'temple', 'consagradas' )|E] e de grandes megalitos nas capitais ducais.",
    16845: "Armas como o goedendag são baratas e fáceis de fabricar, mas são incrivelmente eficazes contra cargas de cavalaria. Sua existência nos permite adaptar nossas milícias para que tenham chances muito melhores ao lutar contra $knight_culture_player_plural_no_tooltip_lowercase$ com armadura.",
    16859: "Os cavaleiros de nossos cavalos hobby celtas são rápidos e ágeis, perfeitos para exploração e ataques em terrenos acidentados, onde os $knight_culture_player_plural_no_tooltip_lowercase$ inimigos relutam em ir.",
    38667: "Paradigma de humildade e piedade, o heroico Rei Shibi foi encarregado de proteger o sábio Agni, que havia sido transformado em um pássaro. Quando um gavião exigiu que ele sacrificasse sua própria carne para salvar a pomba, Shibi se entregou inteiramente de bom grado, mas foi poupado e recompensado com seu filho Kapotaroma. É o sangue deste rei altruísta que corre nas veias de cada [GetDynastyByID('1043008').GetNameNoTooltip].",
    237788: "Adeptos podem [decide|lE] encerrar suas vidas quando estiverem velhos, incapazes ou moribundos; fazer isso não causa nenhuma das penalidades regulares por suicídio",
    241738: "$basque_pagan_adherent$ antigo",
    241739: "$basque_pagan_adherent_plural$ antigos",
    282216: "Este personagem foi deserdado por [Concept( 'dynast', 'chefe da sua dinastia' )|E] e não pode herdar nenhum título de nenhum outro membro da dinastia.",
    287398: "Você ganha o [title|lE] disputado.",
    287401: "Você mantém o [title|lE] disputado.",
}


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


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def validate_packet() -> None:
    summary = read_json(PACKET_SUMMARY)
    rows = read_jsonl(PACKET_JSONL)
    expected_ids = sorted(APPROVED_ALREADY_OK_SEGMENT_IDS + sorted(APPROVED_CORRECTIONS))
    if summary.get("mode") != "read_only_human_review_packet":
        raise SystemExit("packet mode guard failed")
    if int(summary.get("packet_count") or 0) != 13 or len(rows) != 13:
        raise SystemExit("packet count guard failed")
    if sorted(int(row["segment_id"]) for row in rows) != expected_ids:
        raise SystemExit("packet segment ids guard failed")
    if summary.get("production_full_recommended_now") is not False:
        raise SystemExit("production full guard failed")


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


def create_run(conn, total_count: int, inserted_count: int, skipped_count: int, blocked_count: int, timestamp: str) -> int:
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
            total_count,
            1.0,
            inserted_count,
            inserted_count,
            0,
            "completed",
            f"{RULE_VERSION}; skipped_existing={skipped_count}; blocked={blocked_count}; packet={PACKET_SUMMARY}",
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
        "human_confirmed_medium_dynamic_light_holds_batch1",
        "read_only_packet_reviewed_in_chat",
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
            "human-approved medium_dynamic_light holds batch1 learning signal",
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
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_medium_dynamic_light_holds_batch1_learning_ingest"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    snapshot_path = Path(str(base) + "_before_snapshot.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    snapshot_path.write_text(json.dumps(before_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "medium_dynamic_light holds batch1 learning ingest",
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
        "apply_executed=false",
        "production_full_recommended_now=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, snapshot_path


def main() -> None:
    validate_packet()
    reviewed_ids = APPROVED_ALREADY_OK_SEGMENT_IDS + sorted(APPROVED_CORRECTIONS)
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
                records.append({"segment_id": segment_id, "status": "blocked_missing_context", "decision": "approve_already_ok"})
                continue
            current = str(context["current_output_text"] or "")
            if not current.strip():
                blocked_count += 1
                records.append({"segment_id": segment_id, "status": "blocked_blank_current_output", "decision": "approve_already_ok"})
                continue
            existing = existing_candidate(conn, segment_id, sha256_text(current))
            if existing:
                skipped_existing_count += 1
                records.append({"segment_id": segment_id, "status": "skipped_existing", "decision": "approve_already_ok", "existing_candidate_id": int(existing["id"])})
                continue
            plan.append((segment_id, context, current, "correct", MATCH_TYPE_ALREADY_OK, None))

        for segment_id, corrected in sorted(APPROVED_CORRECTIONS.items()):
            context = fetch_context(conn, segment_id)
            if not context:
                blocked_count += 1
                records.append({"segment_id": segment_id, "status": "blocked_missing_context", "decision": "approve_correction"})
                continue
            current = str(context["current_output_text"] or "")
            if protected_tokens(current) != protected_tokens(corrected):
                blocked_count += 1
                records.append(
                    {
                        "segment_id": segment_id,
                        "status": "blocked_token_signature_mismatch",
                        "decision": "approve_correction",
                        "current_tokens": protected_tokens(current),
                        "corrected_tokens": protected_tokens(corrected),
                    }
                )
                continue
            existing = existing_candidate(conn, segment_id, sha256_text(corrected))
            if existing:
                skipped_existing_count += 1
                records.append({"segment_id": segment_id, "status": "skipped_existing", "decision": "approve_correction", "existing_candidate_id": int(existing["id"])})
                continue
            plan.append((segment_id, context, corrected, "semantic_error", MATCH_TYPE_CORRECTION, corrected))

        run_id = create_run(conn, len(reviewed_ids), len(plan), skipped_existing_count, blocked_count, timestamp)
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
                    "decision": "approve_correction" if corrected_text else "approve_already_ok",
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
        "inserted_count": len(plan),
        "inserted_already_ok": inserted_already_ok,
        "inserted_corrections": inserted_corrections,
        "skipped_existing_count": skipped_existing_count,
        "blocked_count": blocked_count,
        "record_status_counts": [{"key": key, "count": count} for key, count in Counter(record["status"] for record in records).most_common()],
        "source_changed": False,
        "output_changed": False,
        "apply_executed": False,
        "lifecycle_executed": False,
        "segment_state_executed": False,
        "reindex_executed": False,
        "production_full_recommended_now": False,
        "next_action": "apply_local_learning_feedback_then_protected_apply_dry_run_for_corrected_segments",
    }
    txt_path, jsonl_path, summary_path, snapshot_path = write_outputs(records, summary, before_snapshot)
    print(f"run_id={run_id}")
    print(f"inserted_count={summary['inserted_count']}")
    print(f"inserted_already_ok={summary['inserted_already_ok']}")
    print(f"inserted_corrections={summary['inserted_corrections']}")
    print(f"blocked_count={blocked_count}")
    print(f"skipped_existing_count={skipped_existing_count}")
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"snapshot={snapshot_path}")


if __name__ == "__main__":
    main()
