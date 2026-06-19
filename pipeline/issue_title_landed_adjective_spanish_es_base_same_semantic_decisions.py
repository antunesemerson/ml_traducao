from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_title_landed_adjective_spanish_es_hint_audit import classify_hint


RULE_VERSION = "issue_title_landed_adjective_spanish_es_base_same_semantic_decisions_v1"
AGENT_KEY = "micro_landed_title_spanish_es_suffix_repair"
QUEUE_STRATEGY = "landed_title_adjective_spanish_es_suffix_repair"
ALLOWED_TIERS = ("b_", "c_", "d_")


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_landed_adjective_spanish_es_base_same_semantic_decisions_queue_{queue_run_id}"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


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


def english_family(english: str | None) -> str:
    value = (english or "").strip().casefold()
    for suffix in ("ian", "ean", "ese", "ais", "ish", "an", "er", "i", "ic"):
        if value.endswith(suffix):
            return f"english_suffix_{suffix}"
    if " " in value:
        return "english_multiword"
    return "english_other"


def starts_with_any_base(hint: str, base: dict[str, Any] | None) -> bool:
    if not base:
        return False
    hint_norm = normalize(hint)
    for key in ("spanish_text", "old_text", "english_text"):
        base_norm = normalize(base.get(key))
        if len(base_norm) >= 4 and hint_norm.startswith(base_norm[: max(4, min(len(base_norm), 8))]):
            return True
    return False


def source_key_stem(source_key: str) -> str:
    return normalize(re.sub(r"^[bcdek]_|_adj$", "", source_key or ""))


def stem_relation(row: dict[str, Any], base: dict[str, Any] | None) -> str:
    hint = str(row.get("repair_hint") or "")
    if not base:
        return "no_base"
    if starts_with_any_base(hint, base):
        return "hint_starts_with_base"
    stem = source_key_stem(str(row.get("source_key") or ""))
    hint_norm = normalize(hint)
    if len(stem) >= 5 and stem and (stem[:5] in hint_norm or hint_norm[:5] in stem):
        return "hint_overlaps_source_key_stem"
    return "semantic_stem_shift"


def is_strict_final_accent_repair(current: str | None, repair: str | None) -> bool:
    current_value = (current or "").strip()
    repair_value = (repair or "").strip()
    if not current_value or not repair_value:
        return False
    if " " in current_value or " " in repair_value:
        return False
    return current_value.endswith("és") and repair_value == f"{current_value[:-2]}ês"


def fetch_base_titles(conn, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    keys = sorted({base_key(str(row.get("source_key") or "")) for row in rows})
    if not keys:
        return {}
    placeholders = ", ".join("?" for _ in keys)
    base_rows = conn.execute(
        f"""
        SELECT source_key, spanish_text, english_text, old_text
        FROM source_segments
        WHERE is_active = 1
          AND source_key IN ({placeholders})
        """,
        keys,
    ).fetchall()
    return {str(row["source_key"]): dict(row) for row in base_rows}


def fetch_rows(conn, *, queue_run_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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

    base_titles = fetch_base_titles(conn, rows)
    selected: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row in rows:
        source_key = str(row.get("source_key") or "")
        base = base_titles.get(base_key(source_key))
        row["base_title"] = base
        row["base_relation"] = base_relation(base)
        row["stem_relation"] = stem_relation(row, base)
        row["english_family"] = english_family(str(row.get("english_text") or ""))
        row["block_reasons"] = []
        if not source_key.startswith(ALLOWED_TIERS):
            row["block_reasons"].append("tier_not_allowed")
        if row["base_relation"] != "base_same_all":
            row["block_reasons"].append(row["base_relation"])
        if row["stem_relation"] != "semantic_stem_shift":
            row["block_reasons"].append(row["stem_relation"])
        if not is_strict_final_accent_repair(row.get("evidence_text"), row.get("repair_hint")):
            row["block_reasons"].append("not_strict_final_es_to_es_circumflex_repair")

        if row["block_reasons"]:
            blocked.append(row)
        else:
            selected.append(row)
    return selected, blocked


def write_outputs(
    *,
    decisions_path: Path,
    report_path: Path,
    queue_run_id: int,
    selected: list[dict[str, Any]],
    blocked: list[dict[str, Any]],
) -> None:
    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            base = row["base_title"] or {}
            handle.write(
                json.dumps(
                    {
                        "queue_run_id": queue_run_id,
                        "queue_item_id": row["id"],
                        "ledger_item_id": row["ledger_item_id"],
                        "segment_id": row["segment_id"],
                        "decision": "needs_repair",
                        "corrected_text": row["repair_hint"],
                        "notes": (
                            f"{RULE_VERSION}; base_key={base_key(str(row.get('source_key') or ''))}; "
                            f"base_relation={row.get('base_relation')}; stem_relation={row.get('stem_relation')}; "
                            f"english_family={row.get('english_family')}; base_old={base.get('old_text')}; "
                            f"base_en={base.get('english_text')}; base_es={base.get('spanish_text')}; "
                            f"current={row.get('evidence_text')}; english={row.get('english_text')}"
                        ),
                        "reviewer": "codex_landed_title_spanish_es_base_same_semantic",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    selected_families = Counter(str(row.get("english_family")) for row in selected)
    selected_reasons = Counter(str(row.get("hint_reason")) for row in selected)
    selected_tiers = Counter(str(row.get("source_key") or "")[:2] for row in selected)
    blocked_reasons = Counter(";".join(row["block_reasons"]) for row in blocked)
    lines = [
        "Spanish -es base-same semantic-stem-shift title adjective decisions",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Selected rows: {len(selected):,}",
        f"Blocked rows: {len(blocked):,}",
        f"Decisions path: {decisions_path}",
        "",
        "Selected by English suffix family:",
        *[f"- {key}: {value:,}" for key, value in selected_families.most_common()],
        "",
        "Selected by original hint reason:",
        *[f"- {key}: {value:,}" for key, value in selected_reasons.most_common()],
        "",
        "Selected by tier:",
        *[f"- {key}: {value:,}" for key, value in selected_tiers.most_common()],
        "",
        "Blocked by guard:",
        *[f"- {key}: {value:,}" for key, value in blocked_reasons.most_common()],
        "",
        "Selected rows:",
    ]
    for row in selected:
        base = row["base_title"] or {}
        lines.append(
            f"- item={row['id']} segment={row['segment_id']} {row.get('source_key')} "
            f"{row.get('evidence_text')} -> {row.get('repair_hint')} "
            f"({row.get('english_text')}; family={row.get('english_family')}; base={base.get('old_text')})"
        )
    lines.extend(["", "Blocked samples:"])
    for row in blocked[:80]:
        base = row.get("base_title") or {}
        lines.append(
            f"- item={row['id']} {row.get('source_key')} blocked={';'.join(row['block_reasons'])} "
            f"{row.get('evidence_text')} -> {row.get('repair_hint')} "
            f"({row.get('english_text')}; base={base.get('old_text')})"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This records learning evidence only.",
            "- It does not write source/output, confirmations, lifecycle policies, or production artifacts.",
            "- The policy is limited to b/c/d title adjectives with stable base text and strict final -és -> -ês repair.",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        selected, blocked = fetch_rows(conn, queue_run_id=selected_queue_run_id)

    decisions_path, report_path = report_paths(settings, selected_queue_run_id)
    write_outputs(
        decisions_path=decisions_path,
        report_path=report_path,
        queue_run_id=selected_queue_run_id,
        selected=selected,
        blocked=blocked,
    )

    print("[issue_title_landed_adjective_spanish_es_base_same_semantic_decisions] Decisions generated")
    print(f"[issue_title_landed_adjective_spanish_es_base_same_semantic_decisions] Rule version: {RULE_VERSION}")
    print(f"[issue_title_landed_adjective_spanish_es_base_same_semantic_decisions] Queue run id: {selected_queue_run_id}")
    print(f"[issue_title_landed_adjective_spanish_es_base_same_semantic_decisions] Selected rows: {len(selected):,}")
    print(f"[issue_title_landed_adjective_spanish_es_base_same_semantic_decisions] Blocked rows: {len(blocked):,}")
    print(f"[issue_title_landed_adjective_spanish_es_base_same_semantic_decisions] Decisions: {decisions_path}")
    print(f"[issue_title_landed_adjective_spanish_es_base_same_semantic_decisions] Report: {report_path}")
    return {
        "queue_run_id": selected_queue_run_id,
        "selected": len(selected),
        "blocked": len(blocked),
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate base-same semantic-stem-shift Spanish -es title adjective repair decisions.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
