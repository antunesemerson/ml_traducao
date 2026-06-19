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


RULE_VERSION = "issue_title_landed_adjective_spanish_es_gazetteer_decisions_v1"
AGENT_KEY = "micro_landed_title_spanish_es_suffix_repair"
QUEUE_STRATEGY = "landed_title_adjective_spanish_es_suffix_repair"
ALLOWED_TIERS = ("b_", "c_", "d_")


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_landed_adjective_spanish_es_gazetteer_decisions_queue_{queue_run_id}"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


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


def starts_with_base_hint(row: dict[str, Any], base: dict[str, Any]) -> bool:
    hint_norm = normalize(row.get("repair_hint"))
    for key in ("spanish_text", "english_text", "old_text"):
        base_norm = normalize(base.get(key))
        if len(base_norm) >= 4 and hint_norm.startswith(base_norm[: max(4, min(len(base_norm), 8))]):
            return True
    return False


def base_texts_match(base: dict[str, Any]) -> bool:
    return normalize(base.get("spanish_text")) == normalize(base.get("english_text")) == normalize(base.get("old_text"))


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
        row["block_reasons"] = []
        if not source_key.startswith(ALLOWED_TIERS):
            row["block_reasons"].append("tier_not_allowed")
        if not row.get("repair_hint"):
            row["block_reasons"].append("missing_repair_hint")
        if " " in str(row.get("evidence_text") or "").strip():
            row["block_reasons"].append("multiword_surface")
        if not base:
            row["block_reasons"].append("missing_base_title")
        elif not base_texts_match(base):
            row["block_reasons"].append("base_texts_do_not_match_all_sources")
        elif not starts_with_base_hint(row, base):
            row["block_reasons"].append("repair_hint_does_not_start_with_base")

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
            base = row["base_title"]
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
                            f"base_old={base.get('old_text')}; base_en={base.get('english_text')}; "
                            f"base_es={base.get('spanish_text')}; hint_reason={row.get('hint_reason')}; "
                            f"current={row.get('evidence_text')}; english={row.get('english_text')}"
                        ),
                        "reviewer": "codex_landed_title_spanish_es_gazetteer",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    selected_reasons = Counter(str(row.get("hint_reason")) for row in selected)
    blocked_reasons = Counter(";".join(row["block_reasons"]) for row in blocked)
    selected_tiers = Counter(str(row.get("source_key") or "")[:2] for row in selected)

    lines = [
        "Spanish -es gazetteer-backed title adjective decisions",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Selected rows: {len(selected):,}",
        f"Blocked rows: {len(blocked):,}",
        f"Decisions path: {decisions_path}",
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
        base = row["base_title"]
        lines.append(
            f"- item={row['id']} segment={row['segment_id']} {row.get('source_key')} "
            f"{row.get('evidence_text')} -> {row.get('repair_hint')} "
            f"({row.get('english_text')}; base={base.get('old_text')})"
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
            "- The policy is intentionally limited to b/c/d title adjectives whose base title text matches across Spanish, English, and old_text.",
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

    print("[issue_title_landed_adjective_spanish_es_gazetteer_decisions] Decisions generated")
    print(f"[issue_title_landed_adjective_spanish_es_gazetteer_decisions] Rule version: {RULE_VERSION}")
    print(f"[issue_title_landed_adjective_spanish_es_gazetteer_decisions] Queue run id: {selected_queue_run_id}")
    print(f"[issue_title_landed_adjective_spanish_es_gazetteer_decisions] Selected rows: {len(selected):,}")
    print(f"[issue_title_landed_adjective_spanish_es_gazetteer_decisions] Blocked rows: {len(blocked):,}")
    print(f"[issue_title_landed_adjective_spanish_es_gazetteer_decisions] Decisions: {decisions_path}")
    print(f"[issue_title_landed_adjective_spanish_es_gazetteer_decisions] Report: {report_path}")
    return {
        "queue_run_id": selected_queue_run_id,
        "selected": len(selected),
        "blocked": len(blocked),
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate gazetteer-backed Spanish -es landed title adjective repair decisions.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
