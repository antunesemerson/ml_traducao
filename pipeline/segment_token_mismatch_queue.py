from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "segment_token_mismatch_queue_v1"
DEFAULT_REVIEW_STATES = ("human_locked", "human_confirmed")


def latest_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE total_segments > 1000
          AND finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete segment_state_runs snapshot found.")
    return int(row["id"])


def parse_review_states(value: str | None, include_auto_confirmed: bool) -> list[str]:
    states = [part.strip() for part in value.split(",") if part.strip()] if value else list(DEFAULT_REVIEW_STATES)
    if include_auto_confirmed and "auto_confirmed" not in states:
        states.append("auto_confirmed")
    return states


def token_family(token: str) -> str:
    if token.startswith("$"):
        return "variable"
    if token.startswith("["):
        if token.startswith("[Concept("):
            return "concept"
        if token.startswith("[Select_CString("):
            return "select_cstring"
        return "bracket"
    if token.startswith("#"):
        return "format"
    if token.startswith("@"):
        return "icon"
    if token == "\\n":
        return "newline"
    return "other"


def classify_diff(missing: list[str], extra: list[str]) -> str:
    families = {token_family(token) for token in [*missing, *extra]}
    if "concept" in {token_family(token) for token in missing} and any(
        token.startswith("[") and "|<STYLE>" in token for token in extra
    ):
        return "concept_replaced_by_direct_link"
    if "select_cstring" in families:
        return "select_cstring_changed"
    if families == {"bracket"}:
        return "bracket_token_changed"
    if families <= {"format"}:
        return "format_tag_changed"
    if families <= {"variable"}:
        return "variable_changed"
    if families <= {"icon"}:
        return "icon_changed"
    if missing and not extra:
        return "token_removed"
    if extra and not missing:
        return "token_added"
    return "mixed_token_change"


def fetch_candidates(
    conn,
    *,
    state_run_id: int,
    review_states: list[str],
    limit: int | None,
    path_like: str | None,
) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in review_states)
    params: list[Any] = [state_run_id, *review_states]
    path_sql = ""
    if path_like:
        path_sql = "AND i.relative_path LIKE ?"
        params.append(path_like)
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            i.segment_id,
            i.relative_path,
            i.source_key,
            i.source_line_number,
            i.final_state,
            i.review_state,
            i.output_state,
            i.priority_score,
            s.spanish_text,
            s.english_text,
            s.old_text,
            o.portuguese_text AS output_text,
            sc.confirmed_text,
            sc.confirmation_level,
            sc.confirmation_source,
            sc.confirmation_label,
            sc.locked
        FROM segment_state_items i
        JOIN source_segments s ON s.id = i.segment_id
        JOIN segment_confirmations sc ON sc.segment_id = i.segment_id
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        WHERE i.run_id = ?
          AND i.needs_output_apply = 1
          AND i.review_state IN ({placeholders})
          AND TRIM(COALESCE(sc.confirmed_text, '')) <> ''
          {path_sql}
        ORDER BY
          i.priority_score DESC,
          i.relative_path,
          i.source_line_number,
          i.segment_id
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        source_tokens = structural_tokens(row["spanish_text"])
        confirmed_tokens = structural_tokens(row["confirmed_text"])
        if source_tokens == confirmed_tokens:
            continue
        missing = sorted((source_tokens - confirmed_tokens).elements())
        extra = sorted((confirmed_tokens - source_tokens).elements())
        enriched = dict(row)
        enriched["missing_tokens"] = missing
        enriched["extra_tokens"] = extra
        enriched["diff_kind"] = classify_diff(missing, extra)
        candidates.append(enriched)
    return candidates


def write_outputs(settings: dict, candidates: list[dict[str, Any]], state_run_id: int, limit: int | None) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = db.utc_now().replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
    base = reports_dir / f"{timestamp}_segment_token_mismatch_queue"
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")
    txt_path = base.with_suffix(".txt")

    fieldnames = [
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "review_state",
        "final_state",
        "diff_kind",
        "missing_tokens",
        "extra_tokens",
        "output_text",
        "confirmed_text",
        "spanish_text",
        "english_text",
        "old_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in candidates:
            writer.writerow(
                {
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_line_number": row["source_line_number"],
                    "source_key": row["source_key"],
                    "review_state": row["review_state"],
                    "final_state": row["final_state"],
                    "diff_kind": row["diff_kind"],
                    "missing_tokens": json.dumps(row["missing_tokens"], ensure_ascii=False),
                    "extra_tokens": json.dumps(row["extra_tokens"], ensure_ascii=False),
                    "output_text": row["output_text"],
                    "confirmed_text": row["confirmed_text"],
                    "spanish_text": row["spanish_text"],
                    "english_text": row["english_text"],
                    "old_text": row["old_text"],
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in candidates:
            payload = {
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_line_number": row["source_line_number"],
                "source_key": row["source_key"],
                "review_state": row["review_state"],
                "final_state": row["final_state"],
                "diff_kind": row["diff_kind"],
                "missing_tokens": row["missing_tokens"],
                "extra_tokens": row["extra_tokens"],
                "spanish_text": row["spanish_text"],
                "english_text": row["english_text"],
                "old_text": row["old_text"],
                "output_text": row["output_text"],
                "confirmed_text": row["confirmed_text"],
                "suggested_review": "verify whether token change is intentional; do not auto-apply without review",
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    kind_counts = Counter(row["diff_kind"] for row in candidates)
    review_counts = Counter(row["review_state"] for row in candidates)
    package_counts: dict[str, int] = defaultdict(int)
    for row in candidates:
        package = row["relative_path"].split("/", 1)[0]
        package_counts[package] += 1

    lines = [
        "Segment token mismatch queue",
        f"Rule version: {RULE_VERSION}",
        f"State run id: {state_run_id}",
        f"Limit: {limit or 'none'}",
        f"Candidates: {len(candidates)}",
        "",
        "By diff kind:",
        *[f"- {key}: {value}" for key, value in kind_counts.most_common()],
        "",
        "By review state:",
        *[f"- {key}: {value}" for key, value in review_counts.most_common()],
        "",
        "Top packages:",
        *[f"- {key}: {value}" for key, value in sorted(package_counts.items(), key=lambda item: item[1], reverse=True)[:20]],
        "",
        "Preview:",
    ]
    for row in candidates[:40]:
        lines.extend(
            [
                f"- segment {row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']} | {row['review_state']} | {row['diff_kind']}",
                f"  MISSING: {json.dumps(row['missing_tokens'], ensure_ascii=False)}",
                f"  EXTRA: {json.dumps(row['extra_tokens'], ensure_ascii=False)}",
                f"  OUTPUT: {short(row['output_text'])}",
                f"  CONFIRMED: {short(row['confirmed_text'])}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main(
    *,
    state_run_id: int | None = None,
    limit: int | None = None,
    path_like: str | None = None,
    review_states_csv: str | None = None,
    include_auto_confirmed: bool = False,
) -> None:
    settings = db.load_settings()
    review_states = parse_review_states(review_states_csv, include_auto_confirmed)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_run_id = state_run_id or latest_state_run_id(conn)
        candidates = fetch_candidates(
            conn,
            state_run_id=selected_run_id,
            review_states=review_states,
            limit=limit,
            path_like=path_like,
        )
    txt_path, csv_path, jsonl_path = write_outputs(settings, candidates, selected_run_id, limit)
    print("[segment_token_mismatch_queue] Queue generated")
    print(f"[segment_token_mismatch_queue] Rule version: {RULE_VERSION}")
    print(f"[segment_token_mismatch_queue] State run id: {selected_run_id}")
    print(f"[segment_token_mismatch_queue] Review states: {', '.join(review_states)}")
    print(f"[segment_token_mismatch_queue] Candidates: {len(candidates)}")
    print(f"[segment_token_mismatch_queue] Report: {txt_path}")
    print(f"[segment_token_mismatch_queue] CSV: {csv_path}")
    print(f"[segment_token_mismatch_queue] JSONL: {jsonl_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a review queue for segment-state token mismatches.")
    parser.add_argument("--state-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--path-like", default=None)
    parser.add_argument("--review-states", default=None)
    parser.add_argument("--include-auto-confirmed", action="store_true")
    args = parser.parse_args()
    main(
        state_run_id=args.state_run_id,
        limit=args.limit,
        path_like=args.path_like,
        review_states_csv=args.review_states,
        include_auto_confirmed=args.include_auto_confirmed,
    )
