from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "auto_confirmation_reopen_audit_v1"

WEAK_AUTO_LABELS = {
    "auto_safe",
    "bulk_likely_confirmable",
    "auto_validated",
    "auto_safe_audit",
}

HUMAN_CURATED_LABELS = {
    "curated_residual_fix",
}

FORMAT_LABEL_MARKERS = {
    "space_after_token",
    "remove_space_before_punctuation",
    "normalize_spanish_punctuation",
    "replace_angled_quotes",
}

TEXT_REPLACEMENT_LABEL_MARKERS = {
    "literal_composite_replacement",
    "inline_literal_replacement",
    "visible_word_replacement",
    "visible_phrase_replacement",
    "visible_composite_replacement",
    "safe_literal_replacement",
}


def display_normalized(text: str | None) -> str:
    value = text or ""
    return value.replace('\\"', '"').replace("\\n", "\n")


def output_match_kind(output_text: str | None, confirmed_text: str | None) -> str:
    if (output_text or "") == (confirmed_text or ""):
        return "exact_match"
    if display_normalized(output_text) == display_normalized(confirmed_text):
        return "display_equivalent_escape_delta"
    return "text_delta"


def latest_state_run(conn) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No complete segment_state_runs snapshot found.")
    return dict(row)


def label_family(confirmation_level: str | None, confirmation_label: str | None) -> str:
    label = confirmation_label or ""
    parts = {part.strip() for part in label.split(";") if part.strip()}
    marker_text = label.replace(";", "+")
    if confirmation_level == "human_confirmed" or parts & HUMAN_CURATED_LABELS:
        return "human_curated"
    if any(marker in marker_text for marker in TEXT_REPLACEMENT_LABEL_MARKERS):
        return "mechanical_text_replacement"
    if any(marker in marker_text for marker in FORMAT_LABEL_MARKERS):
        return "mechanical_formatting"
    if label in WEAK_AUTO_LABELS:
        return "weak_auto_confirmation"
    return "other_auto_confirmation"


def recommendation_for(row: dict[str, Any], family: str) -> str:
    if int(row["high_issue_count"] or 0) > 0 or int(row["issue_count"] or 0) > 0:
        return "manual_review_due_to_issue_signal"
    if row["token_status"] != "ok":
        return "manual_review_due_to_token_status"
    if family == "mechanical_formatting":
        return "sample_for_guarded_lifecycle_policy"
    if family == "mechanical_text_replacement":
        return "sample_before_text_replacement_policy"
    if family == "human_curated":
        return "preserve_or_lock_curated_confirmation"
    if family == "weak_auto_confirmation":
        return "stratified_manual_sampling_required"
    return "manual_sampling_required"


def review_priority(row: dict[str, Any], family: str, recommendation: str) -> float:
    word_count = int(row["word_count"] or 0)
    safe_probability = float(row["model_safe_probability"] or 0)
    confidence = float(row["model_confidence"] or 0)
    base_by_family = {
        "mechanical_formatting": 35.0,
        "mechanical_text_replacement": 55.0,
        "human_curated": 45.0,
        "weak_auto_confirmation": 75.0,
        "other_auto_confirmation": 65.0,
    }
    base = base_by_family.get(family, 65.0)
    if recommendation.startswith("manual_review_due"):
        base += 20.0
    if word_count > 50:
        base += 12.0
    elif word_count > 20:
        base += 8.0
    elif word_count <= 8 and family == "mechanical_formatting":
        base -= 8.0
    if safe_probability < 0.10:
        base += 8.0
    elif safe_probability >= 0.50:
        base -= 8.0
    if confidence < 0.35:
        base -= 4.0
    return round(max(base, 0.0), 3)


def fetch_reopen_rows(conn, state_run: dict[str, Any], *, limit: int | None) -> list[dict[str, Any]]:
    sql = """
        SELECT
            state.id AS state_item_id,
            state.run_id AS state_run_id,
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.source_line_number,
            state.confirmation_level,
            state.confirmation_label,
            state.active_action,
            state.candidate_action,
            state.policy_action,
            state.confirmed_matches_output AS state_confirmed_matches_output,
            score.final_action AS candidate_score_action,
            score.token_status,
            score.risk_class,
            score.word_count,
            score.model_safe_probability,
            score.model_confidence,
            score.issue_count,
            score.high_issue_count,
            score.reasons_json AS candidate_reasons_json,
            source.english_text,
            source.spanish_text,
            output.portuguese_text,
            confirmation.confirmed_text
        FROM segment_state_items state
        JOIN source_segments source ON source.id = state.segment_id
        LEFT JOIN output_segments output ON output.segment_id = state.segment_id
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = state.segment_id
        LEFT JOIN ml_score_items score
          ON score.segment_id = state.segment_id
         AND score.run_id = ?
        WHERE state.run_id = ?
          AND state.final_state = 'reopen_auto_confirmed_autofix'
        ORDER BY
            state.relative_path,
            state.source_line_number,
            state.segment_id
    """
    params: list[Any] = [state_run["candidate_score_run_id"], state_run["id"]]
    if limit:
        sql += "\n        LIMIT ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_audit"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    rows: list[dict[str, Any]],
    counts: Counter[str],
    recommendations: Counter[str],
    state_run: dict[str, Any],
    started_at: datetime,
) -> None:
    fieldnames = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "confirmation_level",
        "confirmation_label",
        "label_family",
        "recommendation",
        "review_priority",
        "word_count",
        "model_safe_probability",
        "model_confidence",
        "token_status",
        "risk_class",
        "candidate_action",
        "active_action",
        "policy_action",
        "state_confirmed_matches_output",
        "exact_confirmed_matches_output",
        "normalized_confirmed_matches_output",
        "output_match_kind",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "output_preview": short(row.get("portuguese_text")),
                "confirmed_preview": short(row.get("confirmed_text")),
                "candidate_reasons_json": row.get("candidate_reasons_json"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    label_counts = Counter(row.get("confirmation_label") or "" for row in rows)
    path_counts = Counter(row.get("relative_path") or "" for row in rows)
    output_match_counts = Counter(row.get("output_match_kind") or "" for row in rows)
    mechanical_rows = [
        row
        for row in rows
        if row["label_family"] in {"mechanical_formatting", "mechanical_text_replacement"}
    ]
    mechanical_rows.sort(key=lambda row: (-float(row["review_priority"]), row["relative_path"], row["source_line_number"] or 0))

    lines = [
        "Auto-confirmation reopen audit",
        f"Rule version: {RULE_VERSION}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"State run id: {state_run['id']}",
        f"Candidate score run id: {state_run['candidate_score_run_id']}",
        "",
        "Summary:",
        f"- Total reopened auto-confirmed autofix rows: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Recommendations:",
        *[f"- {key}: {value:,}" for key, value in recommendations.most_common()],
        "",
        "Output/confirmation match:",
        *[f"- {key}: {value:,}" for key, value in output_match_counts.most_common()],
        "",
        "Top confirmation labels:",
        *[f"- {key or '(empty)'}: {value:,}" for key, value in label_counts.most_common(20)],
        "",
        "Top paths:",
        *[f"- {key}: {value:,}" for key, value in path_counts.most_common(20)],
        "",
        "Mechanical review queue sample:",
    ]
    for row in mechanical_rows[:40]:
        lines.extend(
            [
                (
                    f"- priority={row['review_priority']} | {row['label_family']} | "
                    f"{row['relative_path']}:{row['source_line_number']}:{row['source_key']}"
                ),
                f"  label={row.get('confirmation_label')}; safe_prob={row.get('model_safe_probability')}; word_count={row.get('word_count')}",
                f"  OUT: {short(row.get('portuguese_text'))}",
                f"  CONFIRMED: {short(row.get('confirmed_text'))}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This audit only creates evidence and review queues.",
            "- It does not close lifecycle states, promote policies, alter confirmations, or write output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def insert_run(
    conn,
    *,
    state_run: dict[str, Any],
    counts: Counter[str],
    rows: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    started_at: datetime,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_audit_runs (
            rule_version,
            state_run_id,
            active_score_run_id,
            candidate_score_run_id,
            total_reopen_count,
            mechanical_formatting_count,
            mechanical_text_replacement_count,
            weak_auto_count,
            human_curated_count,
            other_count,
            recommended_mechanical_review_count,
            exact_output_match_count,
            normalized_output_match_count,
            output_delta_count,
            report_path,
            csv_path,
            jsonl_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            state_run["id"],
            state_run["active_score_run_id"],
            state_run["candidate_score_run_id"],
            len(rows),
            counts["mechanical_formatting"],
            counts["mechanical_text_replacement"],
            counts["weak_auto_confirmation"],
            counts["human_curated"],
            counts["other_auto_confirmation"],
            counts["mechanical_formatting"] + counts["mechanical_text_replacement"],
            sum(1 for row in rows if row["output_match_kind"] == "exact_match"),
            sum(1 for row in rows if row["output_match_kind"] == "display_equivalent_escape_delta"),
            sum(1 for row in rows if row["output_match_kind"] == "text_delta"),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    run_id = int(cursor.lastrowid)
    conn.executemany(
        """
        INSERT INTO auto_confirmation_reopen_audit_items (
            run_id,
            state_run_id,
            state_item_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            confirmation_level,
            confirmation_label,
            label_family,
            candidate_action,
            active_action,
            policy_action,
            token_status,
            risk_class,
            word_count,
            model_safe_probability,
            model_confidence,
            issue_count,
            high_issue_count,
            recommendation,
            review_priority,
            state_confirmed_matches_output,
            exact_confirmed_matches_output,
            normalized_confirmed_matches_output,
            output_match_kind,
            reasons_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                row["state_run_id"],
                row["state_item_id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row.get("confirmation_level"),
                row.get("confirmation_label"),
                row["label_family"],
                row.get("candidate_action"),
                row.get("active_action"),
                row.get("policy_action"),
                row.get("token_status"),
                row.get("risk_class"),
                int(row.get("word_count") or 0),
                row.get("model_safe_probability"),
                row.get("model_confidence"),
                int(row.get("issue_count") or 0),
                int(row.get("high_issue_count") or 0),
                row["recommendation"],
                float(row["review_priority"]),
                int(row.get("state_confirmed_matches_output") or 0),
                int(row.get("exact_confirmed_matches_output") or 0),
                int(row.get("normalized_confirmed_matches_output") or 0),
                row.get("output_match_kind"),
                row.get("candidate_reasons_json"),
                now,
            )
            for row in rows
        ],
    )
    return run_id


def main(*, state_run_id: int | None = None, limit: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        state_run = latest_state_run(conn)
        if state_run_id is not None:
            row = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (state_run_id,)).fetchone()
            if not row:
                raise RuntimeError(f"segment_state_runs id not found: {state_run_id}")
            state_run = dict(row)

        rows = fetch_reopen_rows(conn, state_run, limit=limit)
        counts: Counter[str] = Counter()
        recommendations: Counter[str] = Counter()
        for row in rows:
            family = label_family(row.get("confirmation_level"), row.get("confirmation_label"))
            match_kind = output_match_kind(row.get("portuguese_text"), row.get("confirmed_text"))
            recommendation = recommendation_for(row, family)
            row["label_family"] = family
            row["recommendation"] = recommendation
            row["output_match_kind"] = match_kind
            row["exact_confirmed_matches_output"] = 1 if match_kind == "exact_match" else 0
            row["normalized_confirmed_matches_output"] = (
                1 if match_kind in {"exact_match", "display_equivalent_escape_delta"} else 0
            )
            row["review_priority"] = review_priority(row, family, recommendation)
            counts[family] += 1
            recommendations[recommendation] += 1

        txt_path, csv_path, jsonl_path = report_paths(settings)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            rows=rows,
            counts=counts,
            recommendations=recommendations,
            state_run=state_run,
            started_at=started_at,
        )
        run_id = insert_run(
            conn,
            state_run=state_run,
            counts=counts,
            rows=rows,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            started_at=started_at,
        )
        conn.commit()

    print("[auto_confirmation_reopen_audit] Audit generated")
    print(f"[auto_confirmation_reopen_audit] Run id: {run_id}")
    print(f"[auto_confirmation_reopen_audit] State run id: {state_run['id']}")
    print(f"[auto_confirmation_reopen_audit] Total: {len(rows):,}")
    for key, value in counts.most_common():
        print(f"[auto_confirmation_reopen_audit] {key}: {value:,}")
    print(f"[auto_confirmation_reopen_audit] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_audit] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_audit] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "state_run_id": state_run["id"],
        "total": len(rows),
        "counts": dict(counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit auto-confirmed segments reopened by current autofix signals.")
    parser.add_argument("--state-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    main(state_run_id=args.state_run_id, limit=args.limit)
