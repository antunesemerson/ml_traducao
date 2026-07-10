from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "semantic_review_router_accolade_glory_batch27_confirmation_repair_v1"
SEGMENT_TO_CANDIDATE = {
    671: 23740,
    143279: 23741,
}
CORRECTED_TEXT = {
    671: "O [acclaimed_knight|El] não tem o traço [GetTrait('denounced').GetName( GetNullCharacter )|l]",
    143279: "Vidas são ceifadas às centenas, caindo como trigo diante da foice, tantos fios abreviados sob o cenho feroz de assassinos de aluguel cumprindo seu trabalho sangrento.\\n\\nSeu sacrifício certamente, #EMP certamente#! valerá a pena, uma condição necessária para minha glória vindoura. Cada um é um paralelepípedo no caminho do destino, assentado em prol de um objetivo singular.",
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


def fetch_snapshot(conn) -> dict[str, Any]:
    segment_ids = sorted(SEGMENT_TO_CANDIDATE)
    candidate_ids = [SEGMENT_TO_CANDIDATE[segment_id] for segment_id in segment_ids]
    seg_placeholders = ",".join("?" for _ in segment_ids)
    cand_placeholders = ",".join("?" for _ in candidate_ids)
    return {
        "local_learning_candidates": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM local_learning_candidates WHERE id IN ({cand_placeholders}) ORDER BY id",
                tuple(candidate_ids),
            )
        ],
        "segment_confirmations": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM segment_confirmations WHERE segment_id IN ({seg_placeholders}) ORDER BY segment_id, id",
                tuple(segment_ids),
            )
        ],
        "output_segments": [
            dict(row)
            for row in conn.execute(
                f"SELECT segment_id, portuguese_text, output_raw_line FROM output_segments WHERE segment_id IN ({seg_placeholders}) ORDER BY segment_id",
                tuple(segment_ids),
            )
        ],
    }


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any], snapshot: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_review_router_accolade_glory_batch27_confirmation_repair"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    snapshot_path = reports_dir() / f"{base.name}_before_snapshot.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "semantic review router accolade glory batch27 confirmation repair",
        f"rule_version={RULE_VERSION}",
        f"repaired_count={summary['repaired_count']}",
        f"blocked_count={summary['blocked_count']}",
        "source_changed=false",
        "output_changed=false",
        "database_confirmations_changed=true",
        "production_full_recommended_now=false",
        f"next_action={summary['next_action']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, snapshot_path


def main() -> None:
    timestamp = now()
    records: list[dict[str, Any]] = []
    repaired = 0
    blocked = 0
    with db.connect(db.load_settings()) as conn:
        db.ensure_database(conn)
        snapshot = fetch_snapshot(conn)
        for segment_id, candidate_id in SEGMENT_TO_CANDIDATE.items():
            corrected = CORRECTED_TEXT[segment_id]
            candidate = conn.execute(
                """
                SELECT id, segment_id, human_label, local_status, confirmation_synced_at
                FROM local_learning_candidates
                WHERE id = ? AND segment_id = ?
                """,
                (candidate_id, segment_id),
            ).fetchone()
            confirmation = conn.execute(
                """
                SELECT id, confirmation_level, confirmation_source, confirmation_label, locked, candidate_id
                FROM segment_confirmations
                WHERE segment_id = ? AND candidate_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (segment_id, candidate_id),
            ).fetchone()
            reasons: list[str] = []
            if candidate is None:
                reasons.append("missing_candidate")
            if confirmation is None:
                reasons.append("missing_confirmation")
            if confirmation and confirmation["confirmation_level"] != "human_confirmed":
                reasons.append("confirmation_not_human_confirmed")
            if confirmation and confirmation["confirmation_source"] != "local_learning":
                reasons.append("confirmation_source_not_local_learning")
            if confirmation and int(confirmation["locked"] or 0) != 1:
                reasons.append("confirmation_not_locked")
            if reasons:
                blocked += 1
                records.append({"segment_id": segment_id, "candidate_id": candidate_id, "status": "blocked", "block_reasons": reasons})
                continue
            conn.execute(
                """
                UPDATE local_learning_candidates
                SET suggested_text = ?,
                    corrected_text = ?,
                    suggested_hash = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (corrected, corrected, sha256_text(corrected), timestamp, candidate_id),
            )
            conn.execute(
                """
                UPDATE segment_confirmations
                SET confirmed_text = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (corrected, timestamp, confirmation["id"]),
            )
            repaired += 1
            records.append({"segment_id": segment_id, "candidate_id": candidate_id, "status": "repaired", "confirmed_text": corrected})
        conn.commit()
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": timestamp,
        "repaired_count": repaired,
        "blocked_count": blocked,
        "source_changed": False,
        "output_changed": False,
        "database_confirmations_changed": repaired > 0,
        "production_full_recommended_now": False,
        "records": records,
        "next_action": "rerun_correction_dry_run_then_protected_apply",
    }
    txt_path, jsonl_path, summary_path, snapshot_path = write_outputs(records, summary, snapshot)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"before_snapshot={snapshot_path}")
    print(f"repaired_count={repaired}")
    print(f"blocked_count={blocked}")
    print("source_changed=false")
    print("output_changed=false")
    print("database_confirmations_changed=true")


if __name__ == "__main__":
    main()
