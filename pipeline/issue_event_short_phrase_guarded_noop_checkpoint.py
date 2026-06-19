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
from apply_segment_state_updates import short
from issue_dynamic_literal_repair_diagnostic import residual_hits
from local_quality_validator import validate_text


RULE_VERSION = "issue_event_short_phrase_guarded_noop_checkpoint_v4"
POLICY_NAME = "event_short_phrase_requirement_tooltip_noop_shadow_v4"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_event_short_phrase"
SUBPOLICY_NAME = "event_requirement_tooltip_noop"
CHECKPOINT_ACTION = "stage_event_short_phrase_noop_shadow"

EVENT_BLOCK_REASONS = {
    "event_option_or_tooltip_surface_requires_event_microagent",
    "ui_only_v8_blocks_event_short_phrase_surface",
    "ui_only_v9_blocks_event_option_surface",
}

TECHNICAL_KEY_PATTERN = re.compile(
    r"(\.tt(?:\.|_|$)|trigger_failure|not_enough|need_|needs_|required|requirement|"
    r"unavailable|already_|used|unlock|cooldown)",
    re.IGNORECASE,
)
TECHNICAL_TEXT_PATTERN = re.compile(
    r"^(Você precisa (?:de|ter|estar)|Você não tem|Você já|Não pode|Custa|Requer|"
    r"Recompensa de .+ melhorada se|Chance de$)",
    re.IGNORECASE,
)
SUSPICIOUS_PATTERNS = (
    r"\bOusais\b",
    r"não se #EMP me#! pode resistir",
    r"deveria ser cobert[oa]",
    r"\bSer[aá] afetad[oa]\b",
    r"\bIr[aá] afetar\b",
    r"\bSABOR DA OP",
    r"\bDEBUG\b",
    r"#DIE\d",
    r"#BER",
    r"\bPergunto-me que destino lhe aguarda",
    r"\b[A-Za-zÀ-ÿ]+ad\b",
    r"\b\w+ad\s+(?:a|o|as|os|aos)\b",
    r"\b(trinket|insights?|tooltips?|paddocks?|levy)\b",
    r"\bpercussivo\b",
)


def mask_ck3_references(text: str) -> str:
    masked = re.sub(r"\[[^\]]+\]", " ", text)
    masked = re.sub(r"\$[^$]+\$", " ", masked)
    masked = re.sub(r"#[^#\s]+", " ", masked)
    return re.sub(r"\s+", " ", masked).strip()


def latest_guarded_dry_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No semantic short-label guarded expansion dry-run found.")
    return int(row["id"])


def fetch_dry_run(conn, *, dry_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_runs
        WHERE id = ?
        """,
        (dry_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Guarded expansion dry-run not found: {dry_run_id}")
    return dict(row)


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_event_short_phrase_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            dry_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            guard_profile TEXT,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_event_short_phrase_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            dry_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            token_status TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            char_count INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_event_short_phrase_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], dry_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_event_short_phrase_guarded_noop_checkpoint_dry_run_{dry_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_candidates(conn, *, dry_run: dict[str, Any]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in EVENT_BLOCK_REASONS)
    rows = conn.execute(
        f"""
        SELECT
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.block_reason AS queue_bucket,
            item.char_count,
            item.token_count,
            ledger.id AS ledger_item_id,
            ledger.issue_family,
            ledger.issue_kind,
            ledger.evidence_text
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_items item
        JOIN ml_issue_ledger_items ledger
          ON ledger.run_id = ?
         AND ledger.segment_id = item.segment_id
         AND ledger.issue_family = 'semantic_review_router'
        WHERE item.run_id = ?
          AND item.dry_run_allowed = 0
          AND item.block_reason IN ({placeholders})
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        (int(dry_run["ledger_run_id"]), int(dry_run["id"]), *sorted(EVENT_BLOCK_REASONS)),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> tuple[int, str, str]:
    text = row.get("evidence_text") or ""
    source_key = row.get("source_key") or ""
    relative_path = row.get("relative_path") or ""
    token_count = int(row.get("token_count") or 0)

    if not text.strip():
        return 0, "missing_current_text", "missing_text"
    if len(text) > 110:
        return 0, "too_long_for_requirement_tooltip_noop", "too_long"
    if token_count > 3:
        return 0, "too_many_tokens_for_requirement_tooltip_noop", "too_many_tokens"
    if "debug" in relative_path.lower():
        return 0, "debug_path_not_promoted", "debug_path"

    validator_issues = validate_text(text).get("issues") or []
    if validator_issues:
        codes = ",".join(str(issue.get("code") or "quality_issue") for issue in validator_issues[:4])
        return 0, f"local_quality_validator:{codes}", "validator_issue"

    hits = residual_hits(text)
    if hits:
        return 0, "residual_hit:" + ",".join(hits[:6]), "residual_or_literal"

    visible_surface = mask_ck3_references(text)
    for pattern in SUSPICIOUS_PATTERNS:
        if re.search(pattern, visible_surface, flags=re.IGNORECASE):
            return 0, f"suspicious_surface:{pattern}", "suspicious_surface"

    has_technical_key = bool(TECHNICAL_KEY_PATTERN.search(source_key))
    has_technical_text = bool(TECHNICAL_TEXT_PATTERN.search(text))
    if not (has_technical_key and has_technical_text):
        return 0, "not_requirement_tooltip_surface", "not_requirement_tooltip"

    return 1, "", "clean_requirement_tooltip_noop"


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    dry_run: dict[str, Any],
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "agent_key",
        "subpolicy_name",
        "token_status",
        "current_text",
        "corrected_text",
        "char_count",
        "token_count",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue event short phrase guarded noop checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Dry-run id: {dry_run['id']}",
        f"Ledger run id: {dry_run['ledger_run_id']}",
        f"Guard profile: {dry_run.get('guard_profile') or 'unknown'}",
        f"Policy: {POLICY_NAME}",
        f"Policy status: {POLICY_STATUS}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed noop: {counts['allowed']:,}",
        f"- Blocked: {counts['blocked']:,}",
        "",
        "Blocks:",
        *[f"- {key.removeprefix('block:')}: {value:,}" for key, value in counts.items() if key.startswith("block:")],
        "",
        "Allowed samples:",
    ]
    for row in [item for item in rows if item["checkpoint_allowed"]][:50]:
        lines.append(
            f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} | {short(row['current_text'], 180)}"
        )
    lines.extend(["", "Blocked samples:"])
    for row in [item for item in rows if not item["checkpoint_allowed"]][:50]:
        lines.append(
            (
                f"- block={row['block_reason']} segment={row['segment_id']} "
                f"{row['relative_path']}::{row['source_key']} | {short(row['current_text'], 180)}"
            )
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow/no-op checkpoint only: it records that a specialist can explain a clean requirement/tooltip phrase.",
            "- It does not write source/output, does not create confirmations and does not promote production lifecycle.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, dry_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_dry_run = fetch_dry_run(conn, dry_run_id=dry_run_id or latest_guarded_dry_run_id(conn))
        source_rows = fetch_candidates(conn, dry_run=selected_dry_run)
        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for source in source_rows:
            allowed, block_reason, token_status = classify(source)
            counts["allowed" if allowed else "blocked"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            current = source.get("evidence_text") or ""
            classified.append(
                {
                    "ledger_item_id": source["ledger_item_id"],
                    "segment_id": source["segment_id"],
                    "relative_path": source["relative_path"],
                    "source_key": source["source_key"],
                    "source_line_number": source.get("source_line_number"),
                    "agent_key": AGENT_KEY,
                    "issue_family": source["issue_family"],
                    "issue_kind": source["issue_kind"],
                    "queue_bucket": source["queue_bucket"],
                    "subpolicy_name": SUBPOLICY_NAME,
                    "checkpoint_allowed": allowed,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "block_reason": block_reason,
                    "token_status": token_status,
                    "current_text": current,
                    "corrected_text": current if allowed else "",
                    "char_count": len(current),
                    "token_count": int(source.get("token_count") or 0),
                }
            )

        txt_path, csv_path, jsonl_path = report_paths(settings, int(selected_dry_run["id"]))
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_event_short_phrase_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                dry_run_id,
                ledger_run_id,
                guard_profile,
                candidate_count,
                allowed_count,
                blocked_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                int(selected_dry_run["id"]),
                int(selected_dry_run["ledger_run_id"]),
                selected_dry_run.get("guard_profile"),
                len(classified),
                counts["allowed"],
                counts["blocked"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cur.lastrowid)
        for row in classified:
            conn.execute(
                """
                INSERT INTO ml_issue_event_short_phrase_checkpoint_items (
                    run_id,
                    dry_run_id,
                    ledger_run_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    agent_key,
                    issue_family,
                    issue_kind,
                    queue_bucket,
                    subpolicy_name,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    token_status,
                    current_text,
                    corrected_text,
                    char_count,
                    token_count,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    int(selected_dry_run["id"]),
                    int(selected_dry_run["ledger_run_id"]),
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["agent_key"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["queue_bucket"],
                    row["subpolicy_name"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["block_reason"],
                    row["token_status"],
                    row["current_text"],
                    row["corrected_text"],
                    row["char_count"],
                    row["token_count"],
                    now,
                ),
            )
        conn.commit()

    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        run_id=run_id,
        dry_run=selected_dry_run,
        rows=classified,
        counts=counts,
    )
    print("[issue_event_short_phrase_guarded_noop_checkpoint] Checkpoint generated")
    print(f"[issue_event_short_phrase_guarded_noop_checkpoint] Run id: {run_id}")
    print(f"[issue_event_short_phrase_guarded_noop_checkpoint] Dry-run id: {selected_dry_run['id']}")
    print(f"[issue_event_short_phrase_guarded_noop_checkpoint] Candidates: {len(classified):,}")
    print(f"[issue_event_short_phrase_guarded_noop_checkpoint] Allowed noop: {counts['allowed']:,}")
    print(f"[issue_event_short_phrase_guarded_noop_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_event_short_phrase_guarded_noop_checkpoint] Report: {txt_path}")
    return {
        "run_id": run_id,
        "dry_run_id": int(selected_dry_run["id"]),
        "candidates": len(classified),
        "allowed": counts["allowed"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create shadow no-op checkpoint evidence for safe event requirement/tooltips.")
    parser.add_argument("--dry-run-id", type=int, default=None)
    args = parser.parse_args()
    main(dry_run_id=args.dry_run_id)
