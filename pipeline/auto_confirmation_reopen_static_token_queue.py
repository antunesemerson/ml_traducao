from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short
from auto_confirmation_reopen_text_shadow_policy import (
    has_embedded_visible_token_literals,
    has_spanish_custom_localization_helper,
    has_visible_letters_outside_tokens,
    is_spanish_custom_localization_definition,
)


RULE_VERSION = "auto_confirmation_reopen_static_token_queue_v1"
LABEL_FAMILY = "weak_auto_confirmation"
RECOMMENDATION = "static_token_only_shadow_candidate"
SUGGESTED_DECISION = "sample_static_token_only_weak_auto"


def latest_audit_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_audit_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete auto_confirmation_reopen_audit_runs entry found.")
    return int(row["id"])


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return cleaned[:80] or "static_token_queue"


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_{slugify(RECOMMENDATION)}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def fetch_base_rows(conn, *, audit_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            output.portuguese_text,
            confirmation.confirmed_text
        FROM auto_confirmation_reopen_audit_items item
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN output_segments output ON output.segment_id = item.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = item.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        WHERE item.run_id = ?
          AND item.label_family = ?
          AND item.output_match_kind = 'exact_match'
          AND item.token_status = 'ok'
          AND item.issue_count = 0
          AND item.high_issue_count = 0
        ORDER BY
            item.word_count ASC,
            item.review_priority DESC,
            item.relative_path,
            item.source_line_number,
            item.segment_id
        """,
        (audit_run_id, LABEL_FAMILY),
    ).fetchall()
    return [dict(row) for row in rows]


def reviewed_positive_segment_ids(conn) -> set[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT segment_id
        FROM auto_confirmation_reopen_text_review_decisions
        WHERE agent_key = 'weak_auto_empty_or_token_sampler'
          AND text_subfamily = 'weak_auto_empty_or_token_exact'
          AND evidence_label = 'positive_evidence'
        """
    ).fetchall()
    return {int(row["segment_id"]) for row in rows}


def path_group(relative_path: str) -> str:
    if "/" in relative_path:
        return relative_path.split("/", 1)[0]
    return relative_path


def extract_token_shapes(text: str) -> dict[str, int]:
    return {
        "ck3_bracket_count": len(re.findall(r"\[[^\]]*\]", text)),
        "dollar_token_count": len(re.findall(r"\$[^$]*\$", text)),
        "color_token_count": len(re.findall(r"#[A-Za-z0-9_]+|#!", text)),
        "icon_token_count": len(re.findall(r"@[A-Za-z0-9_]+!", text)),
        "concept_count": text.count("Concept("),
        "select_cstring_count": text.count("Select_CString(") + text.count("LocalPlayerString("),
    }


def is_static_token_only_candidate(row: dict[str, Any], *, include_embedded_literals: bool) -> bool:
    text = row.get("confirmed_text") or row.get("portuguese_text") or ""
    return (
        not has_visible_letters_outside_tokens(text)
        and not has_visible_letters_outside_tokens(row.get("english_text") or "")
        and not has_visible_letters_outside_tokens(row.get("spanish_text") or "")
        and (include_embedded_literals or not has_embedded_visible_token_literals(text))
        and not has_spanish_custom_localization_helper(text)
        and not is_spanish_custom_localization_definition(row)
    )


def enrich(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    text = row.get("confirmed_text") or row.get("portuguese_text") or ""
    shapes = extract_token_shapes(text)
    row["path_group"] = path_group(row["relative_path"])
    row["suggested_decision"] = SUGGESTED_DECISION
    row["policy_candidate"] = 0
    row["manual_boundary"] = 0
    row["recommendation"] = RECOMMENDATION
    row["static_token_shape"] = "+".join(key for key, value in shapes.items() if value) or "blank"
    row["reasons_json"] = json.dumps(
        {
            "rule_version": RULE_VERSION,
            "static_token_only": True,
            "path_group": row["path_group"],
            "static_token_shape": row["static_token_shape"],
            **shapes,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return row


def filter_candidates(
    rows: list[dict[str, Any]],
    *,
    include_embedded_literals: bool,
    skip_reviewed_positive: bool,
    reviewed_segment_ids: set[int],
) -> list[dict[str, Any]]:
    candidates = [
        enrich(row)
        for row in rows
        if is_static_token_only_candidate(row, include_embedded_literals=include_embedded_literals)
        and (not skip_reviewed_positive or int(row["segment_id"]) not in reviewed_segment_ids)
    ]
    return candidates


def select_diverse(
    rows: list[dict[str, Any]],
    *,
    limit: int | None,
    per_path_cap: int,
    per_group_cap: int,
) -> list[dict[str, Any]]:
    if limit is None or len(rows) <= limit:
        selected = list(rows)
    else:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[(row.get("path_group") or "", row.get("static_token_shape") or "")].append(row)
        for group_rows in groups.values():
            group_rows.sort(
                key=lambda item: (
                    -float(item.get("review_priority") or 0),
                    item.get("relative_path") or "",
                    int(item.get("source_line_number") or 0),
                    int(item.get("segment_id") or 0),
                )
            )
        ordered_keys = sorted(groups, key=lambda key: (-len(groups[key]), key))
        selected = []
        selected_ids: set[int] = set()
        by_path: Counter[str] = Counter()
        by_group: Counter[str] = Counter()
        while len(selected) < limit:
            added = False
            for key in ordered_keys:
                group = groups[key]
                while group and int(group[0]["segment_id"]) in selected_ids:
                    group.pop(0)
                if not group:
                    continue
                row = group[0]
                path = row.get("relative_path") or ""
                path_root = row.get("path_group") or ""
                if by_path[path] >= per_path_cap or by_group[path_root] >= per_group_cap:
                    group.pop(0)
                    continue
                group.pop(0)
                selected_ids.add(int(row["segment_id"]))
                selected.append(row)
                by_path[path] += 1
                by_group[path_root] += 1
                added = True
                if len(selected) >= limit:
                    break
            if not added:
                remaining = [
                    row
                    for group_rows in groups.values()
                    for row in group_rows
                    if int(row["segment_id"]) not in selected_ids
                ]
                remaining.sort(
                    key=lambda item: (
                        -float(item.get("review_priority") or 0),
                        item.get("relative_path") or "",
                        int(item.get("source_line_number") or 0),
                    )
                )
                for row in remaining:
                    if len(selected) >= limit:
                        break
                    selected_ids.add(int(row["segment_id"]))
                    selected.append(row)
                break
    for rank, row in enumerate(selected, start=1):
        row["queue_rank"] = rank
    return selected


def insert_queue(
    conn,
    *,
    audit_run_id: int,
    rows: list[dict[str, Any]],
    candidate_total: int,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    started_at: datetime,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    match_counts = Counter(row.get("output_match_kind") or "" for row in rows)
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_guarded_queue_runs (
            rule_version,
            audit_run_id,
            label_family,
            recommendation_filter,
            candidate_count,
            selected_count,
            guarded_policy_candidate_count,
            manual_boundary_count,
            exact_output_match_count,
            display_equivalent_count,
            text_delta_count,
            report_path,
            csv_path,
            jsonl_path,
            decisions_template_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            audit_run_id,
            LABEL_FAMILY,
            RECOMMENDATION,
            candidate_total,
            len(rows),
            0,
            0,
            match_counts["exact_match"],
            match_counts["display_equivalent_escape_delta"],
            match_counts["text_delta"],
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    run_id = int(cursor.lastrowid)
    for row in rows:
        item_cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_guarded_queue_items (
                run_id,
                audit_run_id,
                audit_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                confirmation_label,
                label_family,
                recommendation,
                suggested_decision,
                policy_candidate,
                manual_boundary,
                output_match_kind,
                token_status,
                issue_count,
                high_issue_count,
                word_count,
                model_safe_probability,
                model_confidence,
                review_priority,
                queue_rank,
                reasons_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                audit_run_id,
                row["id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row.get("confirmation_label"),
                LABEL_FAMILY,
                RECOMMENDATION,
                row["suggested_decision"],
                int(row["policy_candidate"]),
                int(row["manual_boundary"]),
                row.get("output_match_kind"),
                row.get("token_status"),
                int(row.get("issue_count") or 0),
                int(row.get("high_issue_count") or 0),
                int(row.get("word_count") or 0),
                row.get("model_safe_probability"),
                row.get("model_confidence"),
                float(row.get("review_priority") or 0),
                int(row["queue_rank"]),
                row["reasons_json"],
                now,
            ),
        )
        row["queue_item_id"] = int(item_cursor.lastrowid)
    return run_id


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    run_id: int,
    audit_run_id: int,
    rows: list[dict[str, Any]],
    all_candidates: list[dict[str, Any]],
    include_embedded_literals: bool,
    started_at: datetime,
) -> None:
    fieldnames = [
        "queue_item_id",
        "queue_rank",
        "audit_item_id",
        "segment_id",
        "relative_path",
        "path_group",
        "source_key",
        "source_line_number",
        "confirmation_label",
        "label_family",
        "recommendation",
        "suggested_decision",
        "output_match_kind",
        "token_status",
        "issue_count",
        "high_issue_count",
        "word_count",
        "model_safe_probability",
        "model_confidence",
        "review_priority",
        "static_token_shape",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            payload = {field: row.get(field) for field in fieldnames}
            payload["audit_item_id"] = row.get("id")
            payload["label_family"] = LABEL_FAMILY
            writer.writerow(payload)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "audit_item_id": row.get("id"),
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "output_preview": short(row.get("portuguese_text")),
                "confirmed_preview": short(row.get("confirmed_text")),
                "queue_guidance": (
                    "Check whether an exact weak-auto row with no visible letters outside CK3 tokens "
                    "is safe to learn as positive evidence. Watch for semantic text hidden inside "
                    "Concept/Select_CString tokens and manual exceptions."
                ),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_item_id": row["queue_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "agent_key": "weak_auto_static_token_only_safe_candidate",
                "text_subfamily": "weak_auto_empty_or_token_exact",
                "suggested_decision": SUGGESTED_DECISION,
                "decision": "pending",
                "human_label": "",
                "corrected_text": "",
                "notes": "",
                "english_text": row.get("english_text") or "",
                "spanish_text": row.get("spanish_text") or "",
                "current_output_text": row.get("portuguese_text") or "",
                "confirmed_text": row.get("confirmed_text") or "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    candidate_paths = Counter(row.get("relative_path") or "" for row in all_candidates)
    selected_paths = Counter(row.get("relative_path") or "" for row in rows)
    candidate_shapes = Counter(row.get("static_token_shape") or "" for row in all_candidates)
    selected_shapes = Counter(row.get("static_token_shape") or "" for row in rows)
    lines = [
        "Weak auto static-token-only queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {run_id}",
        f"Audit run id: {audit_run_id}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Static-token-only candidates: {len(all_candidates):,}",
        f"- Selected rows: {len(rows):,}",
        (
            "- Candidate definition: weak auto-confirmation, exact output match, token_status=ok, "
            "no issue signals, no visible letters outside CK3 tokens in English, Spanish, or confirmed/output text"
            + ("; embedded literals allowed for exploration." if include_embedded_literals else "; embedded literals excluded.")
            + "; ES custom-localization helpers excluded."
        ),
        "",
        "Candidate token shapes:",
        *[f"- {key}: {value:,}" for key, value in candidate_shapes.most_common()],
        "",
        "Selected token shapes:",
        *[f"- {key}: {value:,}" for key, value in selected_shapes.most_common()],
        "",
        "Top candidate paths:",
        *[f"- {key}: {value:,}" for key, value in candidate_paths.most_common(20)],
        "",
        "Top selected paths:",
        *[f"- {key}: {value:,}" for key, value in selected_paths.most_common(20)],
        "",
        "Review sample:",
    ]
    for row in rows[:50]:
        lines.extend(
            [
                (
                    f"- #{row['queue_rank']} | {row['static_token_shape']} | "
                    f"{row['relative_path']}:{row['source_line_number']}:{row['source_key']}"
                ),
                (
                    f"  label={row.get('confirmation_label')}; words={row.get('word_count')}; "
                    f"safe_prob={row.get('model_safe_probability')}; priority={row.get('review_priority')}"
                ),
                f"  EN: {short(row.get('english_text'))}",
                f"  ES: {short(row.get('spanish_text'))}",
                f"  OUT: {short(row.get('portuguese_text'))}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This queue records learning evidence only.",
            "- It does not close lifecycle states, alter confirmations, train models, promote policies, or write output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    audit_run_id: int | None = None,
    limit: int | None = 80,
    per_path_cap: int = 8,
    per_group_cap: int = 30,
    include_embedded_literals: bool = False,
    skip_reviewed_positive: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_audit_run_id = audit_run_id or latest_audit_run_id(conn)
        all_rows = fetch_base_rows(conn, audit_run_id=selected_audit_run_id)
        reviewed_segment_ids = reviewed_positive_segment_ids(conn) if skip_reviewed_positive else set()
        all_candidates = filter_candidates(
            all_rows,
            include_embedded_literals=include_embedded_literals,
            skip_reviewed_positive=skip_reviewed_positive,
            reviewed_segment_ids=reviewed_segment_ids,
        )
        rows = select_diverse(
            all_candidates,
            limit=limit,
            per_path_cap=per_path_cap,
            per_group_cap=per_group_cap,
        )
        txt_path, csv_path, jsonl_path, decisions_template_path = report_paths(settings)
        run_id = insert_queue(
            conn,
            audit_run_id=selected_audit_run_id,
            rows=rows,
            candidate_total=len(all_candidates),
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decisions_template_path=decisions_template_path,
            started_at=started_at,
        )
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decisions_template_path=decisions_template_path,
            run_id=run_id,
            audit_run_id=selected_audit_run_id,
            rows=rows,
            all_candidates=all_candidates,
            include_embedded_literals=include_embedded_literals,
            started_at=started_at,
        )
        conn.commit()

    print("[auto_confirmation_reopen_static_token_queue] Queue generated")
    print(f"[auto_confirmation_reopen_static_token_queue] Run id: {run_id}")
    print(f"[auto_confirmation_reopen_static_token_queue] Audit run id: {selected_audit_run_id}")
    print(f"[auto_confirmation_reopen_static_token_queue] Candidates: {len(all_candidates):,}")
    print(f"[auto_confirmation_reopen_static_token_queue] Selected: {len(rows):,}")
    print(f"[auto_confirmation_reopen_static_token_queue] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_static_token_queue] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_static_token_queue] JSONL: {jsonl_path}")
    print(f"[auto_confirmation_reopen_static_token_queue] Decisions template: {decisions_template_path}")
    return {
        "run_id": run_id,
        "audit_run_id": selected_audit_run_id,
        "candidate_total": len(all_candidates),
        "selected_count": len(rows),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a focused queue for weak-auto static-token-only evidence.")
    parser.add_argument("--audit-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--per-path-cap", type=int, default=8)
    parser.add_argument("--per-group-cap", type=int, default=30)
    parser.add_argument(
        "--include-embedded-literals",
        action="store_true",
        help="Include rows with Select_CString/LocalPlayerString/Concept literal payloads for exploratory queues.",
    )
    parser.add_argument(
        "--skip-reviewed-positive",
        action="store_true",
        help="Skip segment ids that already have positive review evidence for the weak-auto empty/token subfamily.",
    )
    args = parser.parse_args()
    main(
        audit_run_id=args.audit_run_id,
        limit=args.limit,
        per_path_cap=args.per_path_cap,
        per_group_cap=args.per_group_cap,
        include_embedded_literals=args.include_embedded_literals,
        skip_reviewed_positive=args.skip_reviewed_positive,
    )
