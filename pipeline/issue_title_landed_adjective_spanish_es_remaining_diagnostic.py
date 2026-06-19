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
from issue_title_landed_adjective_spanish_es_hint_audit import classify_hint


RULE_VERSION = "issue_title_landed_adjective_spanish_es_remaining_diagnostic_v1"
AGENT_KEY = "micro_landed_title_spanish_es_suffix_repair"
QUEUE_STRATEGY = "landed_title_adjective_spanish_es_suffix_repair"


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_landed_adjective_spanish_es_remaining_diagnostic_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_queue_runs
        WHERE agent_key = ?
          AND queue_strategy = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (AGENT_KEY, QUEUE_STRATEGY),
    ).fetchone()
    if row is None:
        raise RuntimeError("No Spanish -es suffix repair queue found.")
    return int(row["id"])


def base_key(source_key: str) -> str:
    return re.sub(r"_adj$", "", source_key or "")


def normalize(value: str | None) -> str:
    text = (value or "").casefold()
    replacements = {
        "\u00e6": "ae",
        "\u0153": "oe",
        "\u00f0": "d",
        "\u00fe": "th",
        "\u0142": "l",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)


def starts_with_any_base(hint: str, base: dict[str, Any] | None, keys: tuple[str, ...]) -> bool:
    if not base:
        return False
    hint_norm = normalize(hint)
    for key in keys:
        base_norm = normalize(base.get(key))
        if len(base_norm) >= 4 and hint_norm.startswith(base_norm[: max(4, min(len(base_norm), 8))]):
            return True
    return False


def english_family(english: str | None) -> str:
    value = (english or "").strip().casefold()
    suffixes = [
        "ian",
        "ean",
        "ese",
        "ais",
        "ish",
        "an",
        "er",
        "ese",
        "i",
        "ic",
        "ese",
    ]
    for suffix in suffixes:
        if value.endswith(suffix):
            return f"english_suffix_{suffix}"
    if " " in value:
        return "english_multiword"
    return "english_other"


def base_relation(base: dict[str, Any] | None) -> str:
    if not base:
        return "missing_base"
    old_norm = normalize(base.get("old_text"))
    spanish_norm = normalize(base.get("spanish_text"))
    english_norm = normalize(base.get("english_text"))
    if old_norm == spanish_norm == english_norm:
        return "base_same_all"
    if old_norm == spanish_norm and old_norm != english_norm:
        return "base_localized_old_spanish"
    if old_norm == english_norm and old_norm != spanish_norm:
        return "base_old_english"
    return "base_mixed"


def stem_relation(row: dict[str, Any], base: dict[str, Any] | None) -> str:
    hint = str(row.get("repair_hint") or "")
    if not base:
        return "no_base"
    if starts_with_any_base(hint, base, ("spanish_text", "old_text", "english_text")):
        return "hint_starts_with_base"
    source_stem = normalize(re.sub(r"^[bcdek]_|_adj$", "", str(row.get("source_key") or "")))
    hint_norm = normalize(hint)
    if source_stem and (source_stem[:5] in hint_norm or hint_norm[:5] in source_stem):
        return "hint_overlaps_source_key_stem"
    return "semantic_stem_shift"


def strategic_group(row: dict[str, Any], base: dict[str, Any] | None) -> str:
    source_key = str(row.get("source_key") or "")
    evidence_text = str(row.get("evidence_text") or "").strip()
    if " " in evidence_text:
        return "multiword_surface"
    if source_key.startswith(("k_", "e_")):
        return "high_tier_context"
    relation = base_relation(base)
    stem = stem_relation(row, base)
    family = english_family(str(row.get("english_text") or ""))
    if relation == "missing_base":
        return "missing_base"
    if stem == "hint_starts_with_base":
        return f"{relation}__base_stem"
    if stem == "hint_overlaps_source_key_stem":
        return f"{relation}__source_stem_overlap__{family}"
    return f"{relation}__semantic_stem_shift__{family}"


def fetch_rows(conn, *, queue_run_id: int) -> list[dict[str, Any]]:
    queue_rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_review_queue_items
        WHERE run_id = ?
          AND agent_key = ?
          AND review_status != 'reviewed'
        ORDER BY queue_bucket, source_key, id
        """,
        (queue_run_id, AGENT_KEY),
    ).fetchall()
    rows: list[dict[str, Any]] = []
    for row in queue_rows:
        item = dict(row)
        evidence = parse_json(item.get("evidence_json"), {})
        item["repair_hint"] = str(evidence.get("repair_hint") or "")
        item["hint_class"], item["hint_reason"] = classify_hint(item)
        rows.append(item)

    base_keys = sorted({base_key(str(row.get("source_key") or "")) for row in rows})
    base_titles: dict[str, dict[str, Any]] = {}
    if base_keys:
        placeholders = ", ".join("?" for _ in base_keys)
        base_rows = conn.execute(
            f"""
            SELECT source_key, spanish_text, english_text, old_text
            FROM source_segments
            WHERE is_active = 1
              AND source_key IN ({placeholders})
            """,
            base_keys,
        ).fetchall()
        base_titles = {str(row["source_key"]): dict(row) for row in base_rows}

    output: list[dict[str, Any]] = []
    for row in rows:
        base = base_titles.get(base_key(str(row.get("source_key") or "")))
        item = dict(row)
        item["base_old_text"] = "" if not base else str(base.get("old_text") or "")
        item["base_english_text"] = "" if not base else str(base.get("english_text") or "")
        item["base_spanish_text"] = "" if not base else str(base.get("spanish_text") or "")
        item["base_relation"] = base_relation(base)
        item["stem_relation"] = stem_relation(item, base)
        item["english_family"] = english_family(str(item.get("english_text") or ""))
        item["strategic_group"] = strategic_group(item, base)
        output.append(item)
    return output


def write_outputs(*, txt_path: Path, csv_path: Path, jsonl_path: Path, queue_run_id: int, rows: list[dict[str, Any]]) -> None:
    group_counts = Counter(row["strategic_group"] for row in rows)
    reason_counts = Counter(row["hint_reason"] for row in rows)
    family_counts = Counter(row["english_family"] for row in rows)
    relation_counts = Counter(row["base_relation"] for row in rows)
    stem_counts = Counter(row["stem_relation"] for row in rows)

    fieldnames = [
        "queue_item_id",
        "segment_id",
        "source_key",
        "strategic_group",
        "hint_reason",
        "english_family",
        "base_relation",
        "stem_relation",
        "current_text",
        "repair_hint",
        "english_text",
        "base_old_text",
        "base_english_text",
        "base_spanish_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "queue_item_id": row["id"],
                    "segment_id": row["segment_id"],
                    "source_key": row.get("source_key"),
                    "strategic_group": row["strategic_group"],
                    "hint_reason": row["hint_reason"],
                    "english_family": row["english_family"],
                    "base_relation": row["base_relation"],
                    "stem_relation": row["stem_relation"],
                    "current_text": row.get("evidence_text"),
                    "repair_hint": row.get("repair_hint"),
                    "english_text": row.get("english_text"),
                    "base_old_text": row.get("base_old_text"),
                    "base_english_text": row.get("base_english_text"),
                    "base_spanish_text": row.get("base_spanish_text"),
                }
            )
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Remaining Spanish -es landed-title adjective diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Rows: {len(rows):,}",
        f"CSV: {csv_path}",
        f"JSONL: {jsonl_path}",
        "",
        "Strategic groups:",
        *[f"- {key}: {value:,}" for key, value in group_counts.most_common()],
        "",
        "Original hint reasons:",
        *[f"- {key}: {value:,}" for key, value in reason_counts.most_common()],
        "",
        "English suffix families:",
        *[f"- {key}: {value:,}" for key, value in family_counts.most_common()],
        "",
        "Base relation:",
        *[f"- {key}: {value:,}" for key, value in relation_counts.most_common()],
        "",
        "Stem relation:",
        *[f"- {key}: {value:,}" for key, value in stem_counts.most_common()],
        "",
        "Samples by strategic group:",
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["strategic_group"], []).append(row)
    for group, group_rows in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        lines.append("")
        lines.append(f"## {group} ({len(group_rows):,})")
        for row in group_rows[:15]:
            lines.append(
                f"- item={row['id']} segment={row['segment_id']} {row.get('source_key')} "
                f"{row.get('evidence_text')} -> {row.get('repair_hint')} | "
                f"en_adj={row.get('english_text')} | base_old={row.get('base_old_text')} | "
                f"base_en={row.get('base_english_text')}"
            )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This diagnostic is read-only and does not ingest decisions.",
            "- Remaining semantic stem shifts require targeted microagents or manual review.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        rows = fetch_rows(conn, queue_run_id=selected_queue_run_id)

    txt_path, csv_path, jsonl_path = report_paths(settings, selected_queue_run_id)
    write_outputs(txt_path=txt_path, csv_path=csv_path, jsonl_path=jsonl_path, queue_run_id=selected_queue_run_id, rows=rows)

    print("[issue_title_landed_adjective_spanish_es_remaining_diagnostic] Diagnostic generated")
    print(f"[issue_title_landed_adjective_spanish_es_remaining_diagnostic] Rule version: {RULE_VERSION}")
    print(f"[issue_title_landed_adjective_spanish_es_remaining_diagnostic] Queue run id: {selected_queue_run_id}")
    print(f"[issue_title_landed_adjective_spanish_es_remaining_diagnostic] Rows: {len(rows):,}")
    print(f"[issue_title_landed_adjective_spanish_es_remaining_diagnostic] Report: {txt_path}")
    print(f"[issue_title_landed_adjective_spanish_es_remaining_diagnostic] CSV: {csv_path}")
    print(f"[issue_title_landed_adjective_spanish_es_remaining_diagnostic] JSONL: {jsonl_path}")
    return {
        "queue_run_id": selected_queue_run_id,
        "rows": len(rows),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose remaining queue-150 Spanish -es landed title adjective repairs.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
