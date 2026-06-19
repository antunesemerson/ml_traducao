from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_context_lane_shadow_validation_queue_v1"
QUEUE_STRATEGY = "short_label_context_lane_shadow_ready_validation"
AGENT_KEY = "micro_custom_localization_fragment"
ISSUE_FAMILY = "short_label_style_microagent"


def latest_shadow_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_context_lane_shadow_policy_runs
        WHERE finished_at IS NOT NULL
          AND shadow_ready_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished context lane shadow run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_context_lane_shadow_validation_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def key_family(source_key: str) -> str:
    if source_key.startswith("RandomConflictDescriptor"):
        return "RandomConflictDescriptor"
    if source_key.startswith("HoFGreeterExchange"):
        return "HoFGreeterExchange"
    if source_key.startswith("RandomCoronationObject"):
        return "RandomCoronationObject"
    if source_key.startswith("CoronationSite"):
        return "CoronationSite"
    if source_key.startswith("RandomHoldingLocation"):
        return "RandomHoldingLocation"
    if source_key.startswith("adjective_"):
        return "adjective"
    if source_key.startswith("creature_"):
        return "creature"
    if source_key.startswith("body_of_water_"):
        return "body_of_water"
    if source_key.startswith("terrain_"):
        return "terrain"
    if source_key.startswith("signature_weapon_"):
        return "signature_weapon"
    if source_key.startswith("compliment_"):
        return "compliment"
    if source_key.startswith("treat_"):
        return "treat"
    if source_key.endswith("_quirk") or "_quirk" in source_key:
        return "personality_quirk"
    match = re.match(r"([A-Za-z]+)", source_key)
    return match.group(1) if match else source_key[:24]


def priority_score(row: dict[str, Any]) -> float:
    text = str(row.get("evidence_text") or "")
    source_key = str(row.get("source_key") or "")
    family = key_family(source_key)
    score = 10.0
    if row.get("relative_path") == "custom_localization/ach_custom_loc_l_spanish.yml":
        score += 5.0
    if family in {"adjective", "HoFGreeterExchange", "RandomConflictDescriptor"}:
        score += 4.0
    if "khan" in source_key.lower() or "khagan" in source_key.lower():
        score += 6.0
    if len(text) <= 12:
        score += 2.0
    if re.search(r"\b(?:canal|kaganal|baronal|condal)\b", text, flags=re.IGNORECASE):
        score += 2.0
    if text.endswith((",", ";", ":")):
        score += 3.0
    return score


def fetch_shadow_run(conn, *, shadow_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_context_lane_shadow_policy_runs
        WHERE id = ?
        """,
        (shadow_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Shadow run not found: {shadow_run_id}")
    return dict(row)


def fetch_candidates(conn, *, shadow_run_id: int, include_existing: bool) -> list[dict[str, Any]]:
    existing_filter = (
        ""
        if include_existing
        else """
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_queue_items queued
              WHERE queued.segment_id = item.segment_id
                AND queued.agent_key = ?
          )
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_decisions decision
              WHERE decision.segment_id = item.segment_id
                AND decision.agent_key = ?
                AND decision.valid = 1
                AND decision.validation_status = 'accepted'
          )
        """
    )
    params: list[Any] = [shadow_run_id]
    if not include_existing:
        params.extend([AGENT_KEY, AGENT_KEY])
    rows = conn.execute(
        f"""
        SELECT *
        FROM ml_issue_short_label_context_lane_shadow_policy_items item
        WHERE item.run_id = ?
          AND item.shadow_status = 'shadow_ready'
          {existing_filter}
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def select_rows(rows: list[dict[str, Any]], *, limit: int, per_file: int, per_family: int) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    for row in sorted(rows, key=lambda item: (-priority_score(item), item["relative_path"], item["source_line_number"] or 0)):
        row["priority_score"] = priority_score(row)
        row["key_family"] = key_family(str(row.get("source_key") or ""))
        buckets[(str(row["relative_path"]), row["key_family"])].append(row)

    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    file_counts: Counter[str] = Counter()
    family_counts: Counter[tuple[str, str]] = Counter()
    bucket_cycle = deque(sorted(buckets, key=lambda key: (-len(buckets[key]), key[0], key[1])))
    while bucket_cycle and len(selected) < limit:
        bucket_key = bucket_cycle.popleft()
        bucket = buckets[bucket_key]
        while bucket:
            row = bucket.popleft()
            segment_id = int(row["segment_id"])
            if segment_id in seen:
                continue
            relative_path = str(row["relative_path"])
            if file_counts[relative_path] >= per_file:
                continue
            if family_counts[bucket_key] >= per_family:
                continue
            selected.append(row)
            seen.add(segment_id)
            file_counts[relative_path] += 1
            family_counts[bucket_key] += 1
            break
        if bucket:
            bucket_cycle.append(bucket_key)
    return selected


def insert_queue(
    conn,
    *,
    shadow_run: dict[str, Any],
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
    limit: int,
    per_file: int,
    per_family: int,
) -> tuple[int, list[dict[str, Any]]]:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    now = db.utc_now()
    bucket = f"short_label_context_shadow_ready:{shadow_run['route_lane']}"
    bucket_counts = Counter(bucket for _ in selected)
    cursor = conn.execute(
        """
        INSERT INTO ml_issue_review_queue_runs (
            rule_version,
            ledger_run_id,
            agent_key,
            issue_family,
            queue_strategy,
            limit_count,
            per_bucket,
            selected_count,
            open_count,
            reviewed_count,
            bucket_counts_json,
            report_path,
            csv_path,
            jsonl_path,
            decisions_template_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            shadow_run.get("diagnostic_run_id"),
            AGENT_KEY,
            ISSUE_FAMILY,
            QUEUE_STRATEGY,
            limit,
            per_file,
            len(selected),
            len(selected),
            json.dumps(dict(bucket_counts), ensure_ascii=False, sort_keys=True),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            now,
            now,
            now,
        ),
    )
    queue_run_id = int(cursor.lastrowid)
    inserted: list[dict[str, Any]] = []
    for row in selected:
        evidence_json = {
            "source": RULE_VERSION,
            "shadow_run_id": shadow_run["id"],
            "shadow_policy_name": shadow_run["policy_name"],
            "route_lane": shadow_run["route_lane"],
            "key_family": row["key_family"],
            "priority_score": row["priority_score"],
            "validation_goal": "validate_shadow_ready_before_checkpoint_expansion",
        }
        item_cursor = conn.execute(
            """
            INSERT INTO ml_issue_review_queue_items (
                run_id,
                ledger_run_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                issue_family,
                issue_kind,
                agent_key,
                queue_bucket,
                priority_score,
                review_status,
                suggested_decision,
                evidence_text,
                evidence_json,
                english_text,
                spanish_text,
                confirmed_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 'validate_shadow_ready_fragment', ?, ?, '', '', '', ?)
            """,
            (
                queue_run_id,
                shadow_run.get("diagnostic_run_id"),
                row["ledger_item_id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                ISSUE_FAMILY,
                "short_or_compact_label_reopened",
                AGENT_KEY,
                bucket,
                row["priority_score"],
                row.get("evidence_text") or "",
                json.dumps(evidence_json, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        inserted.append({**row, "queue_run_id": queue_run_id, "queue_item_id": int(item_cursor.lastrowid), "queue_bucket": bucket})
    return queue_run_id, inserted


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    shadow_run: dict[str, Any],
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    fields = [
        "queue_run_id",
        "queue_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "key_family",
        "priority_score",
        "queue_bucket",
        "classifier_reason",
        "evidence_text",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in selected:
            writer.writerow({field: row.get(field) for field in fields})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")
    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            handle.write(
                json.dumps(
                    {
                        "queue_run_id": queue_run_id,
                        "queue_item_id": row["queue_item_id"],
                        "ledger_item_id": row["ledger_item_id"],
                        "segment_id": row["segment_id"],
                        "decision": "pending",
                        "corrected_text": "",
                        "notes": "",
                        "reviewer": "",
                        "allowed_decisions": ["safe_short_label", "needs_domain_context", "needs_repair", "needs_new_microagent"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    selected_by_file = Counter(str(row["relative_path"]) for row in selected)
    selected_by_family = Counter(f"{row['relative_path']}|{row['key_family']}" for row in selected)
    lines = [
        "Short-label Context Lane Shadow Validation Queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Shadow run id: {shadow_run['id']}",
        f"Route lane: {shadow_run['route_lane']}",
        f"Candidates after filters: {len(candidates):,}",
        f"Selected: {len(selected):,}",
        "",
        "Selected by file:",
    ]
    for key, value in selected_by_file.most_common(30):
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Selected by file/family:"])
    for key, value in selected_by_family.most_common(40):
        lines.append(f"- {key}: {value:,}")
    lines.extend(
        [
            "",
            "Review contract:",
            "- safe_short_label: shadow-ready fragment is genuinely acceptable PT-BR for this custom localization role.",
            "- needs_domain_context: the fragment may be okay but should not be generalized without host context.",
            "- needs_repair: visible wording/semantic mismatch is likely.",
            "- needs_new_microagent: systematic subtype needs a more specific neuron.",
            "",
            "Files:",
            f"- csv: {csv_path}",
            f"- jsonl: {jsonl_path}",
            f"- decisions_template: {decisions_template_path}",
            "",
            "Safety note:",
            "- This queue validates shadow-ready candidates only; it grants no checkpoint or lifecycle authority.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    shadow_run_id: int | None = None,
    limit: int = 160,
    per_file: int = 24,
    per_family: int = 4,
    include_existing: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_shadow_run_id = shadow_run_id or latest_shadow_run_id(conn)
        shadow_run = fetch_shadow_run(conn, shadow_run_id=selected_shadow_run_id)
        candidates = fetch_candidates(conn, shadow_run_id=selected_shadow_run_id, include_existing=include_existing)
        selected = select_rows(candidates, limit=limit, per_file=per_file, per_family=per_family)
        paths = report_paths(settings)
        queue_run_id, inserted = insert_queue(
            conn,
            shadow_run=shadow_run,
            selected=selected,
            paths=paths,
            limit=limit,
            per_file=per_file,
            per_family=per_family,
        )
        conn.commit()

    write_outputs(paths=paths, queue_run_id=queue_run_id, shadow_run=shadow_run, candidates=candidates, selected=inserted)

    print("[issue_short_label_context_lane_shadow_validation_queue] Queue generated")
    print(f"[issue_short_label_context_lane_shadow_validation_queue] Queue run id: {queue_run_id}")
    print(f"[issue_short_label_context_lane_shadow_validation_queue] Shadow run id: {selected_shadow_run_id}")
    print(f"[issue_short_label_context_lane_shadow_validation_queue] Candidates: {len(candidates):,}")
    print(f"[issue_short_label_context_lane_shadow_validation_queue] Selected: {len(inserted):,}")
    print(f"[issue_short_label_context_lane_shadow_validation_queue] Report: {paths[0]}")
    print(f"[issue_short_label_context_lane_shadow_validation_queue] Decisions: {paths[3]}")
    return {
        "queue_run_id": queue_run_id,
        "shadow_run_id": selected_shadow_run_id,
        "candidate_count": len(candidates),
        "selected_count": len(inserted),
        "report_path": str(paths[0]),
        "decisions_template_path": str(paths[3]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create validation queue for shadow-ready short-label context fragments.")
    parser.add_argument("--shadow-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=160)
    parser.add_argument("--per-file", type=int, default=24)
    parser.add_argument("--per-family", type=int, default=4)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        shadow_run_id=args.shadow_run_id,
        limit=args.limit,
        per_file=args.per_file,
        per_family=args.per_family,
        include_existing=args.include_existing,
    )
