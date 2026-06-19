from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "auto_confirmation_reopen_weak_stratified_queue_v1"
LABEL_FAMILY = "weak_auto_confirmation"


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


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_weak_stratified_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def fetch_rows(conn, *, audit_run_id: int) -> list[dict[str, Any]]:
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
        ORDER BY
            item.review_priority DESC,
            item.confirmation_label,
            item.relative_path,
            item.source_line_number,
            item.segment_id
        """,
        (audit_run_id, LABEL_FAMILY),
    ).fetchall()
    return [dict(row) for row in rows]


def path_group(relative_path: str) -> str:
    if "/" in relative_path:
        return relative_path.split("/", 1)[0]
    return relative_path


def word_bucket(word_count: int) -> str:
    if word_count <= 0:
        return "00_empty_or_token"
    if word_count <= 3:
        return "01_tiny"
    if word_count <= 8:
        return "02_short"
    if word_count <= 20:
        return "03_medium"
    if word_count <= 50:
        return "04_long"
    return "05_very_long"


def probability_bucket(value: Any) -> str:
    score = float(value or 0.0)
    if score < 0.05:
        return "p00_lt_005"
    if score < 0.10:
        return "p01_lt_010"
    if score < 0.20:
        return "p02_lt_020"
    if score < 0.40:
        return "p03_lt_040"
    return "p04_gte_040"


def suggested_decision(row: dict[str, Any]) -> tuple[str, int]:
    issue_count = int(row.get("issue_count") or 0)
    high_issue_count = int(row.get("high_issue_count") or 0)
    if high_issue_count > 0:
        return "review_high_issue_weak_auto", 1
    if issue_count > 0:
        return "review_issue_signal_weak_auto", 1
    if row.get("output_match_kind") == "display_equivalent_escape_delta":
        return "sample_escape_delta_weak_auto", 0
    label = row.get("confirmation_label") or ""
    return f"sample_{label}_weak_auto", 0


def enrich(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["path_group"] = path_group(row["relative_path"])
    row["word_bucket"] = word_bucket(int(row.get("word_count") or 0))
    row["probability_bucket"] = probability_bucket(row.get("model_safe_probability"))
    decision, manual_boundary = suggested_decision(row)
    row["suggested_decision"] = decision
    row["policy_candidate"] = 0
    row["manual_boundary"] = manual_boundary
    row["stratum_key"] = "|".join(
        [
            row.get("confirmation_label") or "",
            row.get("recommendation") or "",
            row.get("path_group") or "",
            row.get("word_bucket") or "",
            row.get("output_match_kind") or "",
        ]
    )
    reasons = {
        "path_group": row["path_group"],
        "word_bucket": row["word_bucket"],
        "probability_bucket": row["probability_bucket"],
        "stratum_key": row["stratum_key"],
    }
    row["reasons_json"] = json.dumps(reasons, ensure_ascii=False, sort_keys=True)
    return row


def select_with_caps(
    rows: list[dict[str, Any]],
    *,
    selected_ids: set[int],
    limit: int,
    per_label_cap: int | None = None,
    per_path_cap: int | None = None,
) -> list[dict[str, Any]]:
    picked: list[dict[str, Any]] = []
    by_label: Counter[str] = Counter()
    by_path: Counter[str] = Counter()
    for row in rows:
        if len(picked) >= limit:
            break
        segment_id = int(row["segment_id"])
        if segment_id in selected_ids:
            continue
        label = row.get("confirmation_label") or ""
        path = row.get("relative_path") or ""
        if per_label_cap is not None and by_label[label] >= per_label_cap:
            continue
        if per_path_cap is not None and by_path[path] >= per_path_cap:
            continue
        selected_ids.add(segment_id)
        picked.append(row)
        by_label[label] += 1
        by_path[path] += 1
    return picked


def round_robin_by_stratum(
    rows: list[dict[str, Any]],
    *,
    selected_ids: set[int],
    limit: int,
    current_count: int,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id in selected_ids:
            continue
        key = (
            row.get("confirmation_label") or "",
            row.get("path_group") or "",
            row.get("word_bucket") or "",
            row.get("output_match_kind") or "",
        )
        groups[key].append(row)
    for group_rows in groups.values():
        group_rows.sort(
            key=lambda item: (
                -float(item.get("review_priority") or 0),
                -int(item.get("word_count") or 0),
                item.get("relative_path") or "",
                int(item.get("source_line_number") or 0),
            )
        )
    ordered_keys = sorted(groups, key=lambda key: (-len(groups[key]), key))
    picked: list[dict[str, Any]] = []
    while current_count + len(picked) < limit:
        added = False
        for key in ordered_keys:
            group = groups[key]
            while group and int(group[0]["segment_id"]) in selected_ids:
                group.pop(0)
            if not group:
                continue
            row = group.pop(0)
            selected_ids.add(int(row["segment_id"]))
            picked.append(row)
            added = True
            if current_count + len(picked) >= limit:
                break
        if not added:
            break
    return picked


def select_rows(
    rows: list[dict[str, Any]],
    *,
    total_limit: int,
    issue_limit: int,
    escape_limit: int,
    per_label_target: int,
) -> list[dict[str, Any]]:
    selected_ids: set[int] = set()
    selected: list[dict[str, Any]] = []
    ranked = sorted(
        rows,
        key=lambda item: (
            -int(item.get("high_issue_count") or 0),
            -int(item.get("issue_count") or 0),
            -float(item.get("review_priority") or 0),
            -int(item.get("word_count") or 0),
            item.get("relative_path") or "",
            int(item.get("source_line_number") or 0),
        ),
    )

    issue_rows = [row for row in ranked if int(row.get("issue_count") or 0) > 0 or int(row.get("high_issue_count") or 0) > 0]
    selected.extend(
        select_with_caps(
            issue_rows,
            selected_ids=selected_ids,
            limit=min(issue_limit, total_limit),
            per_label_cap=max(1, issue_limit // 3),
            per_path_cap=6,
        )
    )

    escape_rows = [row for row in ranked if row.get("output_match_kind") == "display_equivalent_escape_delta"]
    selected.extend(
        select_with_caps(
            escape_rows,
            selected_ids=selected_ids,
            limit=max(0, min(escape_limit, total_limit - len(selected))),
            per_label_cap=max(1, escape_limit // 3),
            per_path_cap=4,
        )
    )

    labels = [label for label, _ in Counter(row.get("confirmation_label") or "" for row in rows).most_common()]
    for label in labels:
        if len(selected) >= total_limit:
            break
        current_label_count = sum(1 for row in selected if (row.get("confirmation_label") or "") == label)
        label_target = min(per_label_target, sum(1 for row in rows if (row.get("confirmation_label") or "") == label))
        if current_label_count >= label_target:
            continue
        label_rows = [row for row in ranked if (row.get("confirmation_label") or "") == label]
        needed = min(label_target - current_label_count, total_limit - len(selected))
        selected.extend(
            round_robin_by_stratum(
                label_rows,
                selected_ids=selected_ids,
                limit=len(selected) + needed,
                current_count=len(selected),
            )
        )

    if len(selected) < total_limit:
        selected.extend(
            round_robin_by_stratum(
                ranked,
                selected_ids=selected_ids,
                limit=total_limit,
                current_count=len(selected),
            )
        )

    for rank, row in enumerate(selected, start=1):
        row["queue_rank"] = rank
    return selected


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    rows: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
    audit_run_id: int,
    run_id: int,
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
        "manual_boundary",
        "output_match_kind",
        "token_status",
        "issue_count",
        "high_issue_count",
        "word_count",
        "word_bucket",
        "probability_bucket",
        "model_safe_probability",
        "model_confidence",
        "review_priority",
        "stratum_key",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            payload = {field: row.get(field) for field in fieldnames}
            payload["audit_item_id"] = row.get("id")
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
                    "Review whether the current confirmed/output text is genuinely safe, "
                    "not merely structurally matching. Preserve CK3 tokens exactly."
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
                "confirmation_label": row.get("confirmation_label"),
                "stratum_key": row.get("stratum_key"),
                "suggested_decision": row["suggested_decision"],
                "decision": "pending",
                "allowed_decisions": [
                    "approve_current_confirmed",
                    "reject_current_confirmed",
                    "needs_more_context",
                    "manual_exception_only",
                ],
                "notes": "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    selected_counts = {
        "labels": Counter(row.get("confirmation_label") or "" for row in rows),
        "recommendations": Counter(row.get("recommendation") or "" for row in rows),
        "paths": Counter(row.get("relative_path") or "" for row in rows),
        "path_groups": Counter(row.get("path_group") or "" for row in rows),
        "word_buckets": Counter(row.get("word_bucket") or "" for row in rows),
        "decisions": Counter(row.get("suggested_decision") or "" for row in rows),
    }
    full_counts = {
        "labels": Counter(row.get("confirmation_label") or "" for row in all_rows),
        "recommendations": Counter(row.get("recommendation") or "" for row in all_rows),
        "paths": Counter(row.get("relative_path") or "" for row in all_rows),
        "path_groups": Counter(row.get("path_group") or "" for row in all_rows),
        "word_buckets": Counter(row.get("word_bucket") or "" for row in all_rows),
    }

    lines = [
        "Weak auto-confirmation stratified queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {run_id}",
        f"Audit run id: {audit_run_id}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidate weak-auto rows: {len(all_rows):,}",
        f"- Selected rows: {len(rows):,}",
        f"- Unique strata selected: {len({row['stratum_key'] for row in rows}):,}",
        "",
        "Selected by suggested decision:",
        *[f"- {key}: {value:,}" for key, value in selected_counts["decisions"].most_common()],
        "",
        "Full population by label:",
        *[f"- {key}: {value:,}" for key, value in full_counts["labels"].most_common()],
        "",
        "Selected by label:",
        *[f"- {key}: {value:,}" for key, value in selected_counts["labels"].most_common()],
        "",
        "Selected by recommendation:",
        *[f"- {key}: {value:,}" for key, value in selected_counts["recommendations"].most_common()],
        "",
        "Selected by word bucket:",
        *[f"- {key}: {value:,}" for key, value in selected_counts["word_buckets"].most_common()],
        "",
        "Top full paths:",
        *[f"- {key}: {value:,}" for key, value in full_counts["paths"].most_common(20)],
        "",
        "Top selected paths:",
        *[f"- {key}: {value:,}" for key, value in selected_counts["paths"].most_common(20)],
        "",
        "Review sample:",
    ]
    for row in rows[:40]:
        lines.extend(
            [
                (
                    f"- #{row['queue_rank']} | {row['suggested_decision']} | "
                    f"{row['relative_path']}:{row['source_line_number']}:{row['source_key']}"
                ),
                (
                    f"  label={row.get('confirmation_label')}; rec={row.get('recommendation')}; "
                    f"words={row.get('word_count')}; issue={row.get('issue_count')}; match={row.get('output_match_kind')}"
                ),
                f"  OUT: {short(row.get('portuguese_text'))}",
                f"  CONFIRMED: {short(row.get('confirmed_text'))}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This queue records review evidence only.",
            "- It does not close lifecycle states, alter confirmations, train models, promote policies, or write output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    counts = Counter(row["suggested_decision"] for row in rows)
    match_counts = Counter(row.get("output_match_kind") or "" for row in rows)
    now = datetime.now().isoformat(timespec="seconds")
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
            "stratified_by_label_path_word_issue",
            candidate_total,
            len(rows),
            0,
            counts["review_high_issue_weak_auto"] + counts["review_issue_signal_weak_auto"],
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
                row["label_family"],
                row["recommendation"],
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
                row.get("reasons_json"),
                now,
            ),
        )
        row["queue_item_id"] = int(item_cursor.lastrowid)
    return run_id


def main(
    *,
    audit_run_id: int | None = None,
    total_limit: int = 240,
    issue_limit: int = 80,
    escape_limit: int = 20,
    per_label_target: int = 60,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_audit_run_id = audit_run_id or latest_audit_run_id(conn)
        all_rows = [enrich(row) for row in fetch_rows(conn, audit_run_id=selected_audit_run_id)]
        selected = select_rows(
            all_rows,
            total_limit=total_limit,
            issue_limit=issue_limit,
            escape_limit=escape_limit,
            per_label_target=per_label_target,
        )
        txt_path, csv_path, jsonl_path, decisions_template_path = report_paths(settings)
        run_id = insert_queue(
            conn,
            audit_run_id=selected_audit_run_id,
            rows=selected,
            candidate_total=len(all_rows),
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
            rows=selected,
            all_rows=all_rows,
            audit_run_id=selected_audit_run_id,
            run_id=run_id,
            started_at=started_at,
        )
        conn.commit()

    decisions = Counter(row["suggested_decision"] for row in selected)
    print("[auto_confirmation_reopen_weak_stratified_queue] Queue generated")
    print(f"[auto_confirmation_reopen_weak_stratified_queue] Run id: {run_id}")
    print(f"[auto_confirmation_reopen_weak_stratified_queue] Audit run id: {selected_audit_run_id}")
    print(f"[auto_confirmation_reopen_weak_stratified_queue] Candidates: {len(all_rows):,}")
    print(f"[auto_confirmation_reopen_weak_stratified_queue] Selected: {len(selected):,}")
    for key, value in decisions.most_common():
        print(f"[auto_confirmation_reopen_weak_stratified_queue] {key}: {value:,}")
    print(f"[auto_confirmation_reopen_weak_stratified_queue] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_weak_stratified_queue] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_weak_stratified_queue] JSONL: {jsonl_path}")
    print(f"[auto_confirmation_reopen_weak_stratified_queue] Decisions template: {decisions_template_path}")
    return {
        "run_id": run_id,
        "audit_run_id": selected_audit_run_id,
        "candidate_total": len(all_rows),
        "selected_count": len(selected),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a stratified review queue for weak auto-confirmation reopens.")
    parser.add_argument("--audit-run-id", type=int, default=None)
    parser.add_argument("--total-limit", type=int, default=240)
    parser.add_argument("--issue-limit", type=int, default=80)
    parser.add_argument("--escape-limit", type=int, default=20)
    parser.add_argument("--per-label-target", type=int, default=60)
    args = parser.parse_args()
    main(
        audit_run_id=args.audit_run_id,
        total_limit=args.total_limit,
        issue_limit=args.issue_limit,
        escape_limit=args.escape_limit,
        per_label_target=args.per_label_target,
    )
