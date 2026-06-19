from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_single_combat_signature_weapon_composer_diagnostic_v1"
DEFAULT_DECISION_RUN_ID = 177
SINGLE_COMBAT_FILE = "single_combat_events_l_spanish.yml"

HELPER_RE = re.compile(r"\.Custom2?\(\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
TOKEN_PATTERN_RE = re.compile(r"token_pattern=([^;]+)", re.IGNORECASE)
SIGNATURE_WEAPON_RE = re.compile(r"signature_weapon|SignatureWeapon", re.IGNORECASE)
ES_HELPER_RE = re.compile(r"\bES_[A-Za-z]+")


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def short(value: str | None, limit: int = 220) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def report_paths(decision_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    stem = f"{now_stamp()}_single_combat_signature_weapon_composer_diagnostic_run_{decision_run_id}"
    return reports_dir / f"{stem}.txt", reports_dir / f"{stem}.csv", reports_dir / f"{stem}.jsonl"


def latest_finalized_segment_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise SystemExit("No finalized segment_state_runs row found.")
    return int(row["id"])


def decision_run(conn, decision_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_review_decision_runs
        WHERE id = ?
        """,
        (decision_run_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f"Decision run {decision_run_id} not found.")
    return dict(row)


def source_key_prefix(source_key: str | None) -> str:
    parts = (source_key or "").split(".")
    if len(parts) >= 2 and parts[0] == "single_combat":
        return ".".join(parts[:2])
    return parts[0] if parts and parts[0] else "unknown"


def note_token_pattern(notes: str | None) -> list[str]:
    match = TOKEN_PATTERN_RE.search(notes or "")
    if not match:
        return []
    return sorted({token.strip() for token in match.group(1).split(",") if token.strip()})


def helpers_from_text(*texts: str | None) -> list[str]:
    helpers: set[str] = set()
    for text in texts:
        helpers.update(match.group(1) for match in HELPER_RE.finditer(text or ""))
        helpers.update(ES_HELPER_RE.findall(text or ""))
    return sorted(helpers)


def issue_combos(conn, ledger_run_id: int, segment_ids: list[int]) -> dict[int, str]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            issue_family || ':' || issue_kind AS combo
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        ORDER BY segment_id, issue_family, issue_kind
        """,
        [ledger_run_id, *segment_ids],
    ).fetchall()
    grouped: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        grouped[int(row["segment_id"])].add(str(row["combo"]))
    return {segment_id: ";".join(sorted(values)) for segment_id, values in grouped.items()}


def fetch_candidates(conn, decision_run_id: int, segment_state_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH latest_confirmation AS (
            SELECT sc.*
            FROM segment_confirmations sc
            JOIN (
                SELECT segment_id, MAX(id) AS id
                FROM segment_confirmations
                GROUP BY segment_id
            ) latest ON latest.id = sc.id
        )
        SELECT
            d.id AS decision_id,
            d.run_id AS decision_run_id,
            d.queue_run_id,
            COALESCE(d.ledger_run_id, qi.ledger_run_id) AS ledger_run_id,
            COALESCE(d.ledger_item_id, qi.ledger_item_id) AS ledger_item_id,
            d.segment_id,
            d.relative_path,
            d.source_key,
            d.source_line_number,
            d.issue_family,
            d.issue_kind,
            d.queue_bucket,
            d.normalized_decision,
            d.notes,
            qi.evidence_text,
            qi.english_text AS queue_english_text,
            qi.spanish_text AS queue_spanish_text,
            qi.confirmed_text AS queue_confirmed_text,
            ss.final_state,
            ss.state_group,
            ss.output_state,
            ss.review_state,
            ss.apply_state,
            ss.confirmed_matches_output AS state_confirmed_matches_output,
            ss.needs_output_apply,
            ss.needs_reopen,
            ss.is_closed,
            src.english_text,
            src.spanish_text,
            out.portuguese_text AS output_text,
            out.portuguese_hash AS output_hash,
            lc.id AS confirmation_id,
            lc.confirmed_text,
            lc.confirmation_level,
            lc.confirmation_label,
            lc.confirmation_source
        FROM ml_issue_review_decisions d
        LEFT JOIN ml_issue_review_queue_items qi ON qi.id = d.queue_item_id
        LEFT JOIN segment_state_items ss
          ON ss.run_id = ?
         AND ss.segment_id = d.segment_id
        LEFT JOIN source_segments src
          ON src.id = d.segment_id
         AND src.is_active = 1
        LEFT JOIN output_segments out
          ON out.segment_id = d.segment_id
        LEFT JOIN latest_confirmation lc
          ON lc.segment_id = d.segment_id
        WHERE d.run_id = ?
          AND d.normalized_decision = 'needs_new_microagent'
          AND COALESCE(d.valid, 1) = 1
          AND (
              lower(COALESCE(d.notes, '')) LIKE '%single_combat_signature_weapon_composer%'
              OR COALESCE(d.notes, '') LIKE '%signature_weapon%'
              OR COALESCE(d.notes, '') LIKE '%SignatureWeapon%'
              OR COALESCE(qi.evidence_text, '') LIKE '%signature_weapon%'
              OR COALESCE(qi.evidence_text, '') LIKE '%SignatureWeapon%'
              OR COALESCE(qi.confirmed_text, '') LIKE '%signature_weapon%'
              OR COALESCE(qi.confirmed_text, '') LIKE '%SignatureWeapon%'
              OR COALESCE(src.spanish_text, '') LIKE '%signature_weapon%'
              OR COALESCE(src.spanish_text, '') LIKE '%SignatureWeapon%'
          )
        ORDER BY d.relative_path, d.source_key, d.segment_id
        """,
        (segment_state_run_id, decision_run_id),
    ).fetchall()
    candidates = [dict(row) for row in rows]
    ledger_ids = [int(row["ledger_run_id"]) for row in candidates if row.get("ledger_run_id") is not None]
    ledger_run_id = Counter(ledger_ids).most_common(1)[0][0] if ledger_ids else 0
    combos = issue_combos(conn, ledger_run_id, [int(row["segment_id"]) for row in candidates])

    for row in candidates:
        row["canonical_ledger_run_id"] = ledger_run_id
        row["issue_combinations"] = combos.get(int(row["segment_id"]), "")
        row["helpers"] = sorted(
            set(note_token_pattern(row.get("notes")))
            | set(
                helpers_from_text(
                    row.get("queue_confirmed_text"),
                    row.get("evidence_text"),
                    row.get("confirmed_text"),
                    row.get("spanish_text"),
                    row.get("output_text"),
                )
            )
        )
        row["source_key_prefix"] = source_key_prefix(row.get("source_key"))
        row["confirmation_output_aligned"] = int(
            bool(row.get("confirmed_text"))
            and bool(row.get("output_text"))
            and sha256_text(row.get("confirmed_text")) == sha256_text(row.get("output_text"))
        )
        row["hash_divergent"] = int(
            bool(row.get("confirmed_text"))
            and bool(row.get("output_text"))
            and sha256_text(row.get("confirmed_text")) != sha256_text(row.get("output_text"))
        )
    return candidates


def classify_subpattern(row: dict[str, Any]) -> tuple[str, list[str], str]:
    helpers = set(row.get("helpers") or [])
    text = " ".join(
        str(row.get(key) or "")
        for key in ("queue_confirmed_text", "evidence_text", "confirmed_text", "spanish_text", "output_text", "notes")
    )
    subpatterns: list[str] = []

    if row.get("relative_path") != SINGLE_COMBAT_FILE:
        return (
            "not_single_combat_out_of_scope",
            ["not_single_combat_out_of_scope"],
            "exclude_from_single_combat_composer; route to original domain context review",
        )

    if any(helper.startswith("ES_") for helper in helpers) and (
        "signature_weapon" in helpers or any(helper.startswith("SignatureWeapon") for helper in helpers)
    ):
        subpatterns.append("mixed_signature_weapon_gender_context")
    if any("PresentParticiple" in helper or "ActionThirdPerson" in helper for helper in helpers):
        subpatterns.append("signature_weapon_present_participle_context")
    if any("Attempted" in helper for helper in helpers):
        subpatterns.append("signature_weapon_attempted_attack_context")
    if any("KillType" in helper or "WoundVerb" in helper for helper in helpers):
        subpatterns.append("signature_weapon_kill_or_wound_context")
    if any("EndType" in helper for helper in helpers):
        subpatterns.append("signature_weapon_end_type_context")
    if "signature_weapon" in helpers and re.search(
        r"\b(?:a|o|as|os|da|do|das|dos|de|com|sem|minha|meu|sua|seu|m[aã]o|arma)\b",
        text,
        re.IGNORECASE,
    ):
        subpatterns.append("signature_weapon_noun_article_context")

    if len(subpatterns) >= 2:
        primary = "single_combat_sentence_composer_required"
    elif subpatterns:
        primary = subpatterns[0]
    else:
        primary = "single_combat_sentence_composer_required"
        subpatterns.append(primary)

    recommendation = {
        "mixed_signature_weapon_gender_context": "composer must coordinate signature weapon noun phrase with existing ES_* gender helpers",
        "signature_weapon_present_participle_context": "composer needs verbal/action phrase morphology for weapon-driven motion",
        "signature_weapon_attempted_attack_context": "composer needs attempted attack variants for light/heavy weapon action",
        "signature_weapon_kill_or_wound_context": "composer needs lethal/wound result phrasing tied to weapon action",
        "signature_weapon_end_type_context": "composer needs end-type phrase selection before any closure bridge",
        "signature_weapon_noun_article_context": "composer needs PT-BR article/preposition agreement around the weapon noun",
        "single_combat_sentence_composer_required": "build one sentence-level composer instead of isolated helper substitutions",
    }.get(primary, "manual review required before any microagent or bridge")
    return primary, subpatterns, recommendation


def enrich_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        primary, subpatterns, recommendation = classify_subpattern(row)
        review_status = "already_closed" if int(row.get("is_closed") or 0) else "still_pending"
        if int(row.get("needs_reopen") or 0):
            review_status = "still_pending"
        row.update(
            {
                "subpattern": primary,
                "subpatterns_json": json.dumps(subpatterns, ensure_ascii=False),
                "helpers_json": json.dumps(row.get("helpers") or [], ensure_ascii=False),
                "review_status": review_status,
                "recommendation": recommendation,
                "english_preview": short(row.get("english_text") or row.get("queue_english_text"), 260),
                "spanish_preview": short(row.get("spanish_text") or row.get("queue_spanish_text"), 260),
                "output_preview": short(row.get("output_text") or row.get("confirmed_text") or row.get("queue_confirmed_text"), 260),
            }
        )
        enriched.append(row)
    return enriched


def write_reports(
    rows: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    *,
    decision_run_id: int,
    queue_run_id: int | None,
    ledger_run_id: int,
    segment_state_run_id: int,
) -> None:
    subpattern_counts = Counter(row["subpattern"] for row in rows)
    observed_subpattern_counts: Counter[str] = Counter()
    for row in rows:
        try:
            observed_subpattern_counts.update(json.loads(row.get("subpatterns_json") or "[]"))
        except json.JSONDecodeError:
            observed_subpattern_counts[row["subpattern"]] += 1
    prefix_counts = Counter(row["source_key_prefix"] for row in rows)
    issue_combo_counts = Counter(row.get("issue_combinations") or "(none)" for row in rows)
    status_counts = Counter(row["review_status"] for row in rows)
    alignment_counts = Counter("aligned" if row["confirmation_output_aligned"] else "not_aligned" for row in rows)
    hash_counts = Counter("hash_divergent" if row["hash_divergent"] else "not_divergent" for row in rows)

    lines = [
        "Single combat signature weapon composer diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Decision run id: {decision_run_id}",
        f"Queue run id: {queue_run_id}",
        f"Ledger run id: {ledger_run_id}",
        f"Latest segment_state_run id: {segment_state_run_id}",
        "",
        "Safety:",
        "- Read-only diagnostic: 1",
        "- Production release allowed: 0",
        "- Apply allowed: 0",
        "- Confirmations created: 0",
        "- Source/output writes: 0",
        "",
        "Summary:",
        f"- Total candidates: {len(rows):,}",
        f"- Total in single_combat file: {sum(1 for row in rows if row.get('relative_path') == SINGLE_COMBAT_FILE):,}",
        f"- Out of scope: {sum(1 for row in rows if row.get('subpattern') == 'not_single_combat_out_of_scope'):,}",
        "",
        "Subpatterns:",
    ]
    for key, count in subpattern_counts.most_common():
        lines.append(f"- {key}: {count:,}")

    lines.extend(["", "Observed subpattern labels (multi-label):"])
    for key, count in observed_subpattern_counts.most_common():
        lines.append(f"- {key}: {count:,}")

    lines.extend(["", "Source key prefixes:"])
    for key, count in prefix_counts.most_common():
        lines.append(f"- {key}: {count:,}")

    lines.extend(["", "Issue combinations:"])
    for key, count in issue_combo_counts.most_common(30):
        lines.append(f"- {key}: {count:,}")

    lines.extend(["", "State/alignment:"])
    for key, count in status_counts.most_common():
        lines.append(f"- {key}: {count:,}")
    for key, count in alignment_counts.most_common():
        lines.append(f"- confirmation_output_{key}: {count:,}")
    for key, count in hash_counts.most_common():
        lines.append(f"- {key}: {count:,}")

    lines.extend(["", "Samples by subpattern:"])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if len(grouped[row["subpattern"]]) < 5:
            grouped[row["subpattern"]].append(row)
    for subpattern in sorted(grouped):
        lines.append("")
        lines.append(f"[{subpattern}]")
        for row in grouped[subpattern]:
            lines.append(
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} "
                f"status={row['review_status']} helpers={','.join(row.get('helpers') or [])}"
            )
            lines.append(f"  english: {row['english_preview']}")
            lines.append(f"  spanish: {row['spanish_preview']}")
            lines.append(f"  output: {row['output_preview']}")
            lines.append(f"  recommendation: {row['recommendation']}")

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fieldnames = [
        "decision_run_id",
        "queue_run_id",
        "canonical_ledger_run_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "source_key_prefix",
        "issue_combinations",
        "subpattern",
        "subpatterns_json",
        "helpers_json",
        "review_status",
        "final_state",
        "state_group",
        "output_state",
        "review_state",
        "apply_state",
        "confirmation_output_aligned",
        "hash_divergent",
        "confirmation_id",
        "confirmation_level",
        "confirmation_label",
        "recommendation",
        "english_preview",
        "spanish_preview",
        "output_preview",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({key: row.get(key) for key in fieldnames}, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision-run-id", type=int, default=DEFAULT_DECISION_RUN_ID)
    args = parser.parse_args()

    settings = db.load_settings()
    conn = db.connect(settings)
    run = decision_run(conn, args.decision_run_id)
    segment_state_run_id = latest_finalized_segment_state_run_id(conn)
    rows = enrich_candidates(fetch_candidates(conn, args.decision_run_id, segment_state_run_id))

    ledger_run_id = 0
    if rows:
        ledger_run_id = int(rows[0].get("canonical_ledger_run_id") or 0)
    txt_path, csv_path, jsonl_path = report_paths(args.decision_run_id)
    write_reports(
        rows,
        txt_path,
        csv_path,
        jsonl_path,
        decision_run_id=args.decision_run_id,
        queue_run_id=run.get("queue_run_id"),
        ledger_run_id=ledger_run_id,
        segment_state_run_id=segment_state_run_id,
    )

    print("Single combat signature weapon composer diagnostic complete.")
    print(f"decision_run_id={args.decision_run_id}")
    print(f"queue_run_id={run.get('queue_run_id')}")
    print(f"ledger_run_id={ledger_run_id}")
    print(f"segment_state_run_id={segment_state_run_id}")
    print(f"candidates={len(rows)}")
    print(f"single_combat_candidates={sum(1 for row in rows if row.get('relative_path') == SINGLE_COMBAT_FILE)}")
    print(f"report={txt_path}")
    print(f"csv={csv_path}")
    print(f"jsonl={jsonl_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
