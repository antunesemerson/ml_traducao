from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import apply_local_learning_feedback as learning
import db


RULE_VERSION = "preflight_superseded_human_correction_audit_v1"
NEGATIVE_LABELS = {
    "residual_spanish",
    "structure_error",
    "semantic_error",
    "wrong",
    "rejected",
    "rejected_suggestion",
    "token_mismatch",
}
STOPWORDS = {
    "a",
    "o",
    "as",
    "os",
    "de",
    "da",
    "do",
    "das",
    "dos",
    "em",
    "no",
    "na",
    "nos",
    "nas",
    "por",
    "para",
    "com",
    "que",
    "uma",
    "um",
    "se",
    "e",
    "ou",
    "sua",
    "seu",
    "suas",
    "seus",
    "mais",
    "muito",
    "muita",
    "muitos",
    "muitas",
}
WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ_][A-Za-zÀ-ÖØ-öø-ÿ_'’-]{2,}")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def latest_file(pattern: str) -> Path | None:
    paths = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def short(text: str | None, limit: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def words(text: str | None) -> list[str]:
    out: list[str] = []
    for match in WORD_RE.findall(str(text or "").lower()):
        if match in STOPWORDS or match.startswith("get") or match.startswith("select_cstring"):
            continue
        out.append(match)
    return out


def load_superseded(preflight_path_arg: str | None) -> tuple[Path, dict[int, list[dict[str, Any]]], dict[str, Any]]:
    path = Path(preflight_path_arg) if preflight_path_arg else latest_file("*candidate_generation_preflight_guard_summary.json")
    if not path:
        raise SystemExit("missing candidate_generation_preflight_guard summary")
    summary = read_json(path)
    raw = summary.get("superseded_by_human_correction_segments", {})
    superseded = {int(segment_id): list(reasons) for segment_id, reasons in raw.items()}
    return path, superseded, summary


def fetch_learning_rows(conn: sqlite3.Connection, local_learning_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not local_learning_ids:
        return {}
    placeholders = ",".join("?" for _ in local_learning_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM local_learning_candidates
        WHERE id IN ({placeholders})
        """,
        tuple(local_learning_ids),
    ).fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def fetch_runtime_rows(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    runtime: dict[int, dict[str, Any]] = {
        int(row["segment_id"]): dict(row)
        for row in conn.execute(
            f"""
            SELECT
                s.id AS segment_id,
                s.relative_path,
                s.source_key,
                s.old_text,
                o.portuguese_text AS output_text
            FROM source_segments s
            LEFT JOIN output_segments o ON o.segment_id = s.id
            WHERE s.id IN ({placeholders})
            """,
            tuple(segment_ids),
        )
    }
    confirmations = {
        int(row["segment_id"]): dict(row)
        for row in conn.execute(
            f"""
            SELECT sc.*
            FROM segment_confirmations sc
            JOIN (
                SELECT segment_id, MAX(updated_at) AS updated_at
                FROM segment_confirmations
                WHERE segment_id IN ({placeholders})
                GROUP BY segment_id
            ) latest
              ON latest.segment_id = sc.segment_id
             AND latest.updated_at = sc.updated_at
            """,
            tuple(segment_ids),
        )
    }
    for segment_id, row in runtime.items():
        row["confirmation"] = confirmations.get(segment_id)
    return runtime


def pattern_stats(conn: sqlite3.Connection, keys: list[str]) -> dict[str, dict[str, Any]]:
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"""
        SELECT *
        FROM local_learning_pattern_stats
        WHERE pattern_key IN ({placeholders})
        """,
        tuple(keys),
    ).fetchall()
    return {str(row["pattern_key"]): dict(row) for row in rows}


def classify_record(
    segment_id: int,
    reasons: list[dict[str, Any]],
    learning_rows: dict[int, dict[str, Any]],
    runtime: dict[str, Any],
    stats_by_key: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    local_ids = [int(reason["local_learning_id"]) for reason in reasons if reason.get("local_learning_id")]
    rows = [learning_rows[row_id] for row_id in local_ids if row_id in learning_rows]
    corrected_texts = [str(row.get("corrected_text") or "").strip() for row in rows if str(row.get("corrected_text") or "").strip()]
    labels = sorted({str(row.get("human_label") or "") for row in rows})
    output_text = str(runtime.get("output_text") or "")
    confirmation = runtime.get("confirmation") or {}
    confirmed_text = str(confirmation.get("confirmed_text") or "")
    exact_output_match = any(output_text == text for text in corrected_texts)
    exact_confirmation_match = any(confirmed_text == text for text in corrected_texts)
    correction_exact_match = exact_output_match or exact_confirmation_match

    key_rows: list[dict[str, Any]] = []
    for row in rows:
        for key in learning.pattern_keys(row):
            stat = stats_by_key.get(key)
            if not stat:
                continue
            key_rows.append(
                {
                    "pattern_key": key,
                    "weight_adjustment": float(stat.get("weight_adjustment") or 0.0),
                    "negative_count": int(stat.get("negative_count") or 0),
                    "total_count": int(stat.get("total_count") or 0),
                }
            )
    learned_negative_patterns = [
        item for item in key_rows if item["weight_adjustment"] < 0 or item["negative_count"] > 0
    ]
    has_negative_label = bool(set(labels) & NEGATIVE_LABELS)
    blocked_by_negative_pattern = bool(learned_negative_patterns or has_negative_label)

    potential_overblock = (
        bool(corrected_texts)
        and not correction_exact_match
        and not blocked_by_negative_pattern
        and not exact_output_match
        and not exact_confirmation_match
    )
    if correction_exact_match:
        audit_class = "exact_corrected_segment_match"
    elif blocked_by_negative_pattern:
        audit_class = "learned_negative_or_correction_pattern_block"
    elif potential_overblock:
        audit_class = "potential_valid_candidate_indirectly_blocked"
    else:
        audit_class = "superseded_historical_correction_no_current_match"

    token_delta_words: Counter[str] = Counter()
    for row in rows:
        before_words = set(words(row.get("suggested_text") or row.get("current_output_text") or ""))
        after_words = set(words(row.get("corrected_text") or ""))
        for word in sorted(after_words - before_words):
            token_delta_words[word] += 1

    return {
        "segment_id": segment_id,
        "relative_path": runtime.get("relative_path"),
        "source_key": runtime.get("source_key"),
        "audit_class": audit_class,
        "human_labels": labels,
        "local_learning_ids": local_ids,
        "corrected_text_count": len(set(corrected_texts)),
        "exact_output_match": exact_output_match,
        "exact_confirmation_match": exact_confirmation_match,
        "blocked_by_negative_pattern": blocked_by_negative_pattern,
        "negative_pattern_keys": sorted(
            {item["pattern_key"] for item in learned_negative_patterns},
            key=lambda key: (
                stats_by_key.get(key, {}).get("weight_adjustment", 0),
                key,
            ),
        )[:12],
        "potential_overblock": potential_overblock,
        "output_text": short(output_text),
        "confirmed_text": short(confirmed_text),
        "sample_corrected_text": short(corrected_texts[0] if corrected_texts else ""),
        "top_changed_words": dict(token_delta_words.most_common(8)),
    }


def build_summary(args: argparse.Namespace) -> dict[str, Any]:
    preflight_path, superseded, preflight = load_superseded(args.preflight_summary)
    segment_ids = sorted(superseded)
    local_ids = sorted(
        {
            int(reason["local_learning_id"])
            for reasons in superseded.values()
            for reason in reasons
            if reason.get("local_learning_id")
        }
    )
    with connect_readonly() as conn:
        learning_rows = fetch_learning_rows(conn, local_ids)
        runtime = fetch_runtime_rows(conn, segment_ids)
        all_keys = sorted({key for row in learning_rows.values() for key in learning.pattern_keys(row)})
        stats_by_key = pattern_stats(conn, all_keys)
        records = [
            classify_record(segment_id, superseded[segment_id], learning_rows, runtime.get(segment_id, {}), stats_by_key)
            for segment_id in segment_ids
        ]

    class_counts = Counter(record["audit_class"] for record in records)
    label_counts: Counter[str] = Counter()
    pattern_counts: Counter[str] = Counter()
    word_counts: Counter[str] = Counter()
    for record in records:
        label_counts.update(record["human_labels"])
        pattern_counts.update(record["negative_pattern_keys"])
        word_counts.update(record["top_changed_words"])
    sample_records = sorted(
        records,
        key=lambda item: (
            0 if item["audit_class"] == "potential_valid_candidate_indirectly_blocked" else 1,
            item["segment_id"],
        ),
    )[: args.sample_limit]
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "preflight_summary_path": str(preflight_path),
        "preflight_superseded_segment_count": int(preflight.get("superseded_segment_count") or 0),
        "audited_superseded_segment_count": len(records),
        "local_learning_rows_consulted": len(learning_rows),
        "audit_class_counts": dict(sorted(class_counts.items())),
        "human_label_counts": dict(sorted(label_counts.items())),
        "top_negative_pattern_keys": [
            {"pattern_key": key, "count": count} for key, count in pattern_counts.most_common(20)
        ],
        "top_changed_words": [{"word": key, "count": count} for key, count in word_counts.most_common(30)],
        "potential_overblock_count": class_counts.get("potential_valid_candidate_indirectly_blocked", 0),
        "sample_records": sample_records,
        "apply_recommended_now": False,
        "production_full_recommended_now": False,
        "retarget_recommended_now": False,
        "recommended_next_action": "hold_retarget_until_architecture_or_new_learning_signal",
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_preflight_superseded_human_correction_audit"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in summary["sample_records"]:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "preflight superseded human correction audit",
        f"source={RULE_VERSION}",
        f"preflight_summary_path={summary['preflight_summary_path']}",
        f"preflight_superseded_segment_count={summary['preflight_superseded_segment_count']}",
        f"audited_superseded_segment_count={summary['audited_superseded_segment_count']}",
        f"local_learning_rows_consulted={summary['local_learning_rows_consulted']}",
        f"audit_class_counts={json.dumps(summary['audit_class_counts'], ensure_ascii=False, sort_keys=True)}",
        f"human_label_counts={json.dumps(summary['human_label_counts'], ensure_ascii=False, sort_keys=True)}",
        f"potential_overblock_count={summary['potential_overblock_count']}",
        "",
        "top_negative_pattern_keys:",
        *[
            f"- {item['pattern_key']}: {item['count']}"
            for item in summary["top_negative_pattern_keys"][:12]
        ],
        "",
        "top_changed_words:",
        *[f"- {item['word']}: {item['count']}" for item in summary["top_changed_words"][:15]],
        "",
        f"apply_recommended_now={str(summary['apply_recommended_now']).lower()}",
        f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
        f"retarget_recommended_now={str(summary['retarget_recommended_now']).lower()}",
        f"recommended_next_action={summary['recommended_next_action']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight-summary")
    parser.add_argument("--sample-limit", type=int, default=80)
    args = parser.parse_args()
    summary = build_summary(args)
    txt_path, jsonl_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "audited_superseded_segment_count",
        "local_learning_rows_consulted",
        "audit_class_counts",
        "potential_overblock_count",
        "apply_recommended_now",
        "production_full_recommended_now",
        "retarget_recommended_now",
        "recommended_next_action",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
