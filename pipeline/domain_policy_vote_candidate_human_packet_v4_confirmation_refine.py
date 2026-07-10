from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SEGMENT_ID = 34890
OLD_TEXT = "Este personagem abandonou seus deveres de tutor para se concentrar na administração do reino."
NEW_TEXT = "Este personagem abandonou seus deveres de tutoria para se concentrar na administração do reino."
QUEUE_SOURCE = "domain-policy-vote-candidate-human-packet-v4"
ORIGIN = "human_confirmed_domain_policy_vote_candidate_human_packet_v4"
MATCH_TYPE = "domain_policy_vote_candidate_human_packet_v4_correction"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch_snapshot(conn) -> dict[str, Any]:
    return {
        "segment_confirmations": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM segment_confirmations WHERE segment_id = ? ORDER BY id",
                (SEGMENT_ID,),
            )
        ],
        "local_learning_candidates": [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM local_learning_candidates
                WHERE segment_id = ?
                  AND queue_source = ?
                  AND origin = ?
                  AND match_type = ?
                ORDER BY id
                """,
                (SEGMENT_ID, QUEUE_SOURCE, ORIGIN, MATCH_TYPE),
            )
        ],
    }


def evaluate(snapshot: dict[str, Any]) -> tuple[str, list[str], int | None, int | None]:
    reasons: list[str] = []
    confirmations = snapshot["segment_confirmations"]
    candidates = snapshot["local_learning_candidates"]
    matching_confirmations = [
        row
        for row in confirmations
        if row.get("confirmation_level") == "human_confirmed"
        and row.get("confirmation_source") == "local_learning"
        and row.get("confirmation_label") == "correct"
        and int(row.get("locked") or 0) == 1
        and row.get("confirmed_text") == OLD_TEXT
    ]
    matching_candidates = [
        row
        for row in candidates
        if row.get("suggested_text") == OLD_TEXT
        and row.get("human_label") == "correct"
        and row.get("local_status") == "high_confidence"
    ]
    if len(matching_confirmations) != 1:
        reasons.append(f"expected_one_matching_confirmation_got_{len(matching_confirmations)}")
    if len(matching_candidates) != 1:
        reasons.append(f"expected_one_matching_candidate_got_{len(matching_candidates)}")
    confirmation_id = int(matching_confirmations[0]["id"]) if len(matching_confirmations) == 1 else None
    candidate_id = int(matching_candidates[0]["id"]) if len(matching_candidates) == 1 else None
    if confirmation_id and candidate_id and int(matching_confirmations[0].get("candidate_id") or 0) != candidate_id:
        reasons.append("confirmation_candidate_mismatch")
    return ("ready" if not reasons else "blocked"), reasons, confirmation_id, candidate_id


def write_report(mode: str, snapshot: dict[str, Any], status: str, reasons: list[str], confirmation_id: int | None, candidate_id: int | None) -> Path:
    path = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_human_packet_v4_confirmation_refine_{mode}_summary.json"
    payload = {
        "schema_version": 1,
        "mode": mode,
        "segment_id": SEGMENT_ID,
        "status": status,
        "block_reasons": reasons,
        "confirmation_id": confirmation_id,
        "candidate_id": candidate_id,
        "old_text": OLD_TEXT,
        "new_text": NEW_TEXT,
        "source_changed": False,
        "output_changed": False,
        "database_confirmation_changed": mode == "apply" and status == "ready",
        "snapshot_before": snapshot,
        "production_full_recommended_now": False,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "apply" if args.apply else "dry_run"
    timestamp = now()
    with db.connect(db.load_settings()) as conn:
        db.ensure_database(conn)
        snapshot = fetch_snapshot(conn)
        status, reasons, confirmation_id, candidate_id = evaluate(snapshot)
        if args.apply:
            if status != "ready" or confirmation_id is None or candidate_id is None:
                raise SystemExit(f"apply blocked: {reasons}")
            conn.execute(
                """
                UPDATE segment_confirmations
                SET confirmed_text = ?, updated_at = ?
                WHERE id = ?
                """,
                (NEW_TEXT, timestamp, confirmation_id),
            )
            conn.execute(
                """
                UPDATE local_learning_candidates
                SET suggested_text = ?, suggested_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (NEW_TEXT, sha256_text(NEW_TEXT), timestamp, candidate_id),
            )
            conn.commit()
        report = write_report(mode, snapshot, status, reasons, confirmation_id, candidate_id)
    print(f"summary={report}")
    print(f"mode={mode}")
    print(f"status={status}")
    print(f"block_reasons={json.dumps(reasons, ensure_ascii=False)}")
    print("source_changed=false")
    print("output_changed=false")
    print("production_full_recommended_now=false")


if __name__ == "__main__":
    main()
