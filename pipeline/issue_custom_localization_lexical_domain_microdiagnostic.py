from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_custom_localization_lexical_domain_microdiagnostic_v1"
DIAGNOSTIC_NAME = "custom_localization_lexical_domain_followup_v1"
PRODUCTION_RELEASE_ALLOWED = 0
DEFAULT_DECISION_RUN_ID = 111
CUSTOM_REGIONAL_PATH = "custom_localization/regional_custom_loc_l_spanish.yml"


FLOWER_REPAIR_MAP = {
    "aster": ("áster", "accented_ptbr_flower_name"),
    "astros": ("ásteres", "wrong_semantic_plural_stars_to_flower_plural"),
    "gardenia": ("gardênia", "accented_ptbr_flower_name"),
    "gardenias": ("gardênias", "accented_ptbr_flower_plural"),
    "papoila": ("papoula", "pt_pt_variant_to_ptbr"),
    "papoilas": ("papoulas", "pt_pt_variant_to_ptbr_plural"),
    "peonía": ("peônia", "spanish_surface_to_ptbr"),
    "peonías": ("peônias", "spanish_surface_to_ptbr_plural"),
    "dragão": ("boca-de-leão", "literal_surface_to_ptbr_flower_common_name"),
}

FLOWER_REVIEW_MAP = {
    "a?afr?es": ("açafrões", "mojibake_or_encoding_damage_requires_manual_confirmation"),
    "jade vine": ("trepadeira-de-jade", "english_residue_or_preserved_botanical_name_review"),
    "planta jarra": ("planta-jarro", "botanical_common_name_hyphenation_review"),
    "plantas jarra": ("plantas-jarro", "botanical_common_name_plural_hyphenation_review"),
    "telipogón": ("telipogon", "botanical_genus_preservation_review"),
    "telipogones": ("telipogons", "botanical_genus_preservation_plural_review"),
}


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def canonical(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def latest_decision_run_id(conn, decision_run_id: int | None) -> int:
    if decision_run_id is not None:
        return decision_run_id
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_decision_runs
        WHERE queue_run_id = 115
          AND accepted_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No queue 115 decision run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any], decision_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_custom_localization_lexical_domain_microdiagnostic_decision_run_{decision_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_custom_localization_lexical_domain_microdiagnostic_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            diagnostic_name TEXT NOT NULL,
            diagnostic_status TEXT NOT NULL,
            source_decision_run_id INTEGER NOT NULL,
            source_review_queue_run_id INTEGER,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            flower_candidate_count INTEGER NOT NULL DEFAULT 0,
            dog_candidate_count INTEGER NOT NULL DEFAULT 0,
            high_confidence_repair_count INTEGER NOT NULL DEFAULT 0,
            domain_review_count INTEGER NOT NULL DEFAULT 0,
            new_microagent_candidate_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            category_counts_json TEXT,
            confidence_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_custom_localization_lexical_domain_microdiagnostic_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source_decision_run_id INTEGER NOT NULL,
            source_review_queue_run_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            english_text TEXT,
            spanish_text TEXT,
            confirmed_text TEXT,
            proposed_text TEXT,
            category TEXT NOT NULL,
            confidence TEXT NOT NULL,
            recommended_decision TEXT NOT NULL,
            microagent_key TEXT NOT NULL,
            reason TEXT NOT NULL,
            source_evidence TEXT NOT NULL,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_custom_localization_lexical_domain_microdiagnostic_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_custom_loc_lexical_domain_items_run
        ON ml_issue_custom_localization_lexical_domain_microdiagnostic_items(run_id, category, confidence);

        CREATE INDEX IF NOT EXISTS idx_custom_loc_lexical_domain_items_segment
        ON ml_issue_custom_localization_lexical_domain_microdiagnostic_items(segment_id);
        """
    )


def latest_confirmed_segments(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH latest_conf AS (
            SELECT c.*
            FROM segment_confirmations c
            JOIN (
                SELECT segment_id, MAX(id) AS max_id
                FROM segment_confirmations
                GROUP BY segment_id
            ) latest ON latest.segment_id = c.segment_id
                    AND latest.max_id = c.id
        )
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            c.confirmed_text
        FROM source_segments s
        LEFT JOIN latest_conf c ON c.segment_id = s.id
        WHERE s.relative_path = ?
          AND (
              s.source_key LIKE 'flower_type_%'
              OR s.source_key LIKE 'dog_type_%'
          )
          AND s.is_active = 1
        ORDER BY s.source_key
        """,
        (CUSTOM_REGIONAL_PATH,),
    ).fetchall()
    return [dict(row) for row in rows]


def source_review_queue_run_id(conn, decision_run_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT queue_run_id
        FROM ml_issue_review_decision_runs
        WHERE id = ?
        """,
        (decision_run_id,),
    ).fetchone()
    if row is None:
        return None
    value = row["queue_run_id"]
    return int(value) if value is not None else None


def reviewed_blocker_segments(conn, decision_run_id: int) -> set[int]:
    rows = conn.execute(
        """
        SELECT segment_id
        FROM ml_issue_review_decisions
        WHERE run_id = ?
          AND valid = 1
          AND normalized_decision IN ('needs_repair', 'needs_new_microagent', 'needs_domain_context')
        """,
        (decision_run_id,),
    ).fetchall()
    return {int(row["segment_id"]) for row in rows}


def classify_flower(row: dict[str, Any], reviewed_blockers: set[int]) -> dict[str, Any] | None:
    current = normalize_text(row.get("confirmed_text"))
    source_key = str(row.get("source_key") or "")
    is_plural_key = source_key.endswith("_plural")

    if current in FLOWER_REPAIR_MAP:
        proposed, reason = FLOWER_REPAIR_MAP[current]
        return {
            "proposed_text": proposed,
            "category": "ptbr_flower_lexical_repair",
            "confidence": "high",
            "recommended_decision": "needs_repair",
            "microagent_key": "micro_ptbr_flower_lexicon",
            "reason": reason,
            "source_evidence": "queue_115_blocker" if int(row["segment_id"]) in reviewed_blockers else "pattern_extension",
        }

    if current in FLOWER_REVIEW_MAP:
        proposed, reason = FLOWER_REVIEW_MAP[current]
        confidence = "medium"
        if "mojibake" in reason or "genus_preservation" in reason:
            confidence = "review"
        return {
            "proposed_text": proposed,
            "category": "ptbr_flower_domain_review",
            "confidence": confidence,
            "recommended_decision": "needs_domain_context",
            "microagent_key": "micro_ptbr_flower_lexicon",
            "reason": reason,
            "source_evidence": "pattern_extension",
        }

    if is_plural_key and current == "jasmim":
        return {
            "proposed_text": "jasmins",
            "category": "ptbr_flower_lexical_repair",
            "confidence": "high",
            "recommended_decision": "needs_repair",
            "microagent_key": "micro_ptbr_flower_lexicon",
            "reason": "plural_key_has_singular_surface",
            "source_evidence": "pattern_extension",
        }
    return None


def classify_dog_type(row: dict[str, Any], reviewed_blockers: set[int]) -> dict[str, Any] | None:
    current = normalize_text(row.get("confirmed_text"))
    english = normalize_text(row.get("english_text"))
    spanish = normalize_text(row.get("spanish_text"))
    source_key = str(row.get("source_key") or "")
    if not source_key.startswith("dog_type_"):
        return None

    if english and english == spanish and current and current != english:
        if " " in english and int(row["segment_id"]) in reviewed_blockers:
            return {
                "proposed_text": row.get("english_text") or "",
                "category": "native_dog_type_preservation",
                "confidence": "high",
                "recommended_decision": "needs_new_microagent",
                "microagent_key": "micro_native_dog_type_preservation",
                "reason": "english_and_spanish_preserve_native_multiword_type_but_pt_translates_literally",
                "source_evidence": "queue_115_blocker",
            }
        return {
            "proposed_text": row.get("english_text") or "",
            "category": "native_dog_type_morphology_review",
            "confidence": "review",
            "recommended_decision": "needs_domain_context",
            "microagent_key": "micro_native_dog_type_preservation",
            "reason": "english_and_spanish_preserve_breed_name_but_pt_morphologically_localizes",
            "source_evidence": "pattern_extension",
        }
    return None


def build_items(conn, decision_run_id: int) -> list[dict[str, Any]]:
    reviewed_blockers = reviewed_blocker_segments(conn, decision_run_id)
    items: list[dict[str, Any]] = []
    seen_segments: set[int] = set()
    for row in latest_confirmed_segments(conn):
        classification: dict[str, Any] | None = None
        source_key = str(row.get("source_key") or "")
        if source_key.startswith("flower_type_"):
            classification = classify_flower(row, reviewed_blockers)
        elif source_key.startswith("dog_type_"):
            classification = classify_dog_type(row, reviewed_blockers)
        if not classification:
            continue
        segment_id = int(row["segment_id"])
        if segment_id in seen_segments:
            continue
        seen_segments.add(segment_id)
        item = {
            **row,
            **classification,
        }
        items.append(item)
    return items


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    decision_run_id: int,
    review_queue_run_id: int | None,
    items: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "english_text",
        "spanish_text",
        "confirmed_text",
        "proposed_text",
        "category",
        "confidence",
        "recommended_decision",
        "microagent_key",
        "reason",
        "source_evidence",
        "production_release_allowed",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            writer.writerow(item)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps({field: item.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    samples = [
        (
            f"- {item['category']} | {item['confidence']} | segment={item['segment_id']} "
            f"{item['source_key']} | current={item.get('confirmed_text')!r} -> proposed={item.get('proposed_text')!r} "
            f"| {item['reason']}"
        )
        for item in items[:80]
    ]
    lines = [
        "Custom Localization Lexical/Domain Microdiagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Diagnostic name: {DIAGNOSTIC_NAME}",
        f"Run id: {run_id}",
        f"Source decision run id: {decision_run_id}",
        f"Source review queue run id: {review_queue_run_id or 'unknown'}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Total candidates: {len(items):,}",
        f"- Flower candidates: {counts['family_flower']:,}",
        f"- Dog-type candidates: {counts['family_dog']:,}",
        f"- High-confidence repairs: {counts['confidence_high']:,}",
        f"- Domain/context review: {counts['confidence_review'] + counts['confidence_medium']:,}",
        f"- New microagent candidates: {counts['recommended_needs_new_microagent']:,}",
        "",
        "Category counts:",
        *[f"- {key.removeprefix('category_')}: {value:,}" for key, value in counts.items() if key.startswith("category_")],
        "",
        "Confidence counts:",
        *[f"- {key.removeprefix('confidence_')}: {value:,}" for key, value in counts.items() if key.startswith("confidence_")],
        "",
        "Recommended decision counts:",
        *[f"- {key.removeprefix('recommended_')}: {value:,}" for key, value in counts.items() if key.startswith("recommended_")],
        "",
        "Samples:",
        *samples,
        "",
        "Safety note:",
        "- This diagnostic creates learning evidence only.",
        "- It does not write source/output, confirmations, lifecycle policies, or production rules.",
        "- High-confidence means suitable for a focused repair queue/checkpoint, not automatic output application.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, decision_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    selected_decision_run_id: int
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_decision_run_id = latest_decision_run_id(conn, decision_run_id)
        review_queue_run_id = source_review_queue_run_id(conn, selected_decision_run_id)
        items = build_items(conn, selected_decision_run_id)
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_decision_run_id)

        counts: Counter[str] = Counter()
        for item in items:
            item["production_release_allowed"] = PRODUCTION_RELEASE_ALLOWED
            if str(item["source_key"]).startswith("flower_type_"):
                counts["family_flower"] += 1
            elif str(item["source_key"]).startswith("dog_type_"):
                counts["family_dog"] += 1
            counts[f"category_{item['category']}"] += 1
            counts[f"confidence_{item['confidence']}"] += 1
            counts[f"recommended_{item['recommended_decision']}"] += 1

        cur = conn.execute(
            """
            INSERT INTO ml_issue_custom_localization_lexical_domain_microdiagnostic_runs (
                rule_version,
                diagnostic_name,
                diagnostic_status,
                source_decision_run_id,
                source_review_queue_run_id,
                total_candidates,
                flower_candidate_count,
                dog_candidate_count,
                high_confidence_repair_count,
                domain_review_count,
                new_microagent_candidate_count,
                production_release_allowed,
                category_counts_json,
                confidence_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                updated_at
            )
            VALUES (?, ?, 'shadow_diagnostic', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                DIAGNOSTIC_NAME,
                selected_decision_run_id,
                review_queue_run_id,
                len(items),
                counts["family_flower"],
                counts["family_dog"],
                counts["confidence_high"],
                counts["confidence_medium"] + counts["confidence_review"],
                counts["recommended_needs_new_microagent"],
                PRODUCTION_RELEASE_ALLOWED,
                json.dumps({k.removeprefix("category_"): v for k, v in counts.items() if k.startswith("category_")}, ensure_ascii=False, sort_keys=True),
                json.dumps({k.removeprefix("confidence_"): v for k, v in counts.items() if k.startswith("confidence_")}, ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                started_at,
            ),
        )
        run_id = int(cur.lastrowid)
        now = db.utc_now()
        for item in items:
            conn.execute(
                """
                INSERT INTO ml_issue_custom_localization_lexical_domain_microdiagnostic_items (
                    run_id,
                    source_decision_run_id,
                    source_review_queue_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    english_text,
                    spanish_text,
                    confirmed_text,
                    proposed_text,
                    category,
                    confidence,
                    recommended_decision,
                    microagent_key,
                    reason,
                    source_evidence,
                    production_release_allowed,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_decision_run_id,
                    review_queue_run_id,
                    int(item["segment_id"]),
                    item["relative_path"],
                    item["source_key"],
                    item.get("source_line_number"),
                    item.get("english_text"),
                    item.get("spanish_text"),
                    item.get("confirmed_text"),
                    item.get("proposed_text"),
                    item["category"],
                    item["confidence"],
                    item["recommended_decision"],
                    item["microagent_key"],
                    item["reason"],
                    item["source_evidence"],
                    PRODUCTION_RELEASE_ALLOWED,
                    now,
                ),
            )

        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            decision_run_id=selected_decision_run_id,
            review_queue_run_id=review_queue_run_id,
            items=items,
            counts=counts,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE ml_issue_custom_localization_lexical_domain_microdiagnostic_runs
            SET finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (finished_at, finished_at, run_id),
        )
        conn.commit()

    print("[issue_custom_localization_lexical_domain_microdiagnostic] Diagnostic generated")
    print(f"[issue_custom_localization_lexical_domain_microdiagnostic] Run id: {run_id}")
    print(f"[issue_custom_localization_lexical_domain_microdiagnostic] Source decision run id: {selected_decision_run_id}")
    print(f"[issue_custom_localization_lexical_domain_microdiagnostic] Candidates: {len(items):,}")
    print(f"[issue_custom_localization_lexical_domain_microdiagnostic] Report: {txt_path}")
    return {
        "run_id": run_id,
        "decision_run_id": selected_decision_run_id,
        "candidate_count": len(items),
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose custom-localization lexical/domain follow-ups from composition review blockers.")
    parser.add_argument("--decision-run-id", type=int, default=DEFAULT_DECISION_RUN_ID)
    args = parser.parse_args()
    main(decision_run_id=args.decision_run_id)
