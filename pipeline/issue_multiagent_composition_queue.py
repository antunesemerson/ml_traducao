from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import issue_review_queue


RULE_VERSION = "issue_multiagent_composition_queue_v1"
DEFAULT_AGENT_KEY = "composition_coordinator_v1"
DEFAULT_LIMIT = 120
DEFAULT_PER_BUCKET = 20


def latest_id(conn, table_name: str, where_sql: str = "1 = 1") -> int | None:
    row = conn.execute(
        f"""
        SELECT id
        FROM {table_name}
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def percent(part: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return float(part) / float(total) * 100.0


def short(value: str | None, limit: int = 240) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_multiagent_composition_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        reports_dir / f"{base.name}_decisions_template.jsonl",
    )


def latest_partial_coverage_run(conn, partial_run_id: int | None) -> dict[str, Any]:
    if partial_run_id is None:
        partial_run_id = latest_id(conn, "ml_issue_partial_coverage_runs", "finished_at IS NOT NULL")
    if partial_run_id is None:
        raise RuntimeError("No finished ml_issue_partial_coverage_runs found.")
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_partial_coverage_runs
        WHERE id = ?
        """,
        (partial_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Partial coverage run not found: {partial_run_id}")
    return dict(row)


def family_maturity(conn, partial_run_id: int) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            issue_families_json,
            covered_families_json
        FROM ml_issue_partial_coverage_items
        WHERE run_id = ?
        """,
        (partial_run_id,),
    ).fetchall()
    maturity: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"segments": 0, "issue_items": 0, "covered_items": 0}
    )
    for row in rows:
        issue_families = parse_json_dict(row["issue_families_json"])
        covered_families = parse_json_dict(row["covered_families_json"])
        for family, count in issue_families.items():
            try:
                issue_count = int(count or 0)
            except (TypeError, ValueError):
                issue_count = 0
            maturity[family]["segments"] += 1
            maturity[family]["issue_items"] += issue_count
            try:
                covered_count = int(covered_families.get(family) or 0)
            except (TypeError, ValueError):
                covered_count = 0
            maturity[family]["covered_items"] += covered_count

    normalized: dict[str, dict[str, Any]] = {}
    for family, values in maturity.items():
        issue_items = int(values["issue_items"] or 0)
        covered_items = int(values["covered_items"] or 0)
        normalized[family] = {
            **values,
            "has_checkpoint_evidence": covered_items > 0,
            "family_coverage_rate": (covered_items / issue_items) if issue_items else 0.0,
        }
    return normalized


def load_segment_ledger_items(conn, *, ledger_run_id: int, segment_ids: set[int]) -> dict[int, list[dict[str, Any]]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            confirmation.confirmed_text
        FROM ml_issue_ledger_items item
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN segment_confirmations confirmation ON confirmation.id = (
            SELECT c.id
            FROM segment_confirmations c
            WHERE c.segment_id = item.segment_id
            ORDER BY c.updated_at DESC, c.id DESC
            LIMIT 1
        )
        WHERE item.run_id = ?
          AND item.status = 'open'
          AND item.segment_id IN ({placeholders})
        ORDER BY item.segment_id, item.issue_family, item.issue_kind, item.id
        """,
        (ledger_run_id, *sorted(segment_ids)),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["segment_id"])].append(dict(row))
    return grouped


def existing_composition_segments(conn, *, agent_key: str, include_existing: bool) -> set[int]:
    if include_existing:
        return set()
    rows = conn.execute(
        """
        SELECT DISTINCT item.segment_id
        FROM ml_issue_review_queue_items item
        JOIN ml_issue_review_queue_runs run ON run.id = item.run_id
        WHERE run.queue_strategy = 'multi_issue_composition_opportunity'
          AND item.agent_key = ?
        """,
        (agent_key,),
    ).fetchall()
    return {int(row["segment_id"]) for row in rows}


def segment_cluster(relative_path: str, open_families: dict[str, Any]) -> str:
    path = relative_path or ""
    families = set(open_families)
    if "nicknames" in path:
        return "nickname_cluster"
    if path.startswith("activities/") or "/activities/" in path:
        return "activity_cluster"
    if "laamp_contract" in path:
        return "laamp_contract_cluster"
    if path.startswith("event_localization/"):
        return "event_cluster"
    if "long_text_composer" in families:
        return "long_text_cluster"
    if "autofix_unknown_microagent" in families:
        return "unknown_autofix_cluster"
    return "general_cluster"


def opportunity_bucket(
    *,
    total_issues: int,
    family_count: int,
    mature_family_count: int,
    unknown_families: list[str],
    cluster: str,
) -> str:
    all_mature = family_count > 0 and mature_family_count == family_count
    unknown_set = set(unknown_families)
    if all_mature and total_issues <= 2:
        return "all_mature_2_issue"
    if all_mature and total_issues <= 4:
        return "all_mature_3_4_issue"
    if all_mature:
        return "all_mature_high_issue"
    if "autofix_unknown_microagent" in unknown_set:
        return "autofix_unknown_blocker"
    if "long_text_composer" in unknown_set:
        return "long_text_composer_blocker"
    if cluster == "nickname_cluster":
        return "nickname_multiagent_cluster"
    if cluster == "activity_cluster":
        return "activity_multiagent_cluster"
    if mature_family_count >= 4:
        return "high_maturity_mixed_cluster"
    return "mixed_near_closure"


def chosen_ledger_item(
    ledger_items: list[dict[str, Any]],
    *,
    unknown_families: list[str],
    mature_families: list[str],
) -> dict[str, Any] | None:
    if not ledger_items:
        return None
    priority_family_order = [
        *unknown_families,
        "long_text_composer",
        "autofix_unknown_microagent",
        "semantic_review_router",
        "dynamic_ck3_expression_microagent",
        "gender_token_microagent",
        "spanish_residual_microagent",
        "short_label_style_microagent",
        *mature_families,
    ]
    order_index = {family: index for index, family in enumerate(priority_family_order)}
    return sorted(
        ledger_items,
        key=lambda row: (
            order_index.get(str(row.get("issue_family") or ""), 999),
            -float(row.get("confidence_score") or 0.0),
            int(row.get("id") or 0),
        ),
    )[0]


def composition_priority(row: dict[str, Any]) -> float:
    evidence = parse_json_dict(row.get("evidence_json"))
    composition = evidence.get("_multiagent_composition", {})
    mature_ratio = float(composition.get("mature_family_ratio") or 0.0)
    issue_mature_ratio = float(composition.get("issue_mature_ratio") or 0.0)
    total = int(composition.get("total_issue_count") or 0)
    unknown_count = int(composition.get("unknown_family_count") or 0)
    score = 0.0
    score += mature_ratio * 800
    score += issue_mature_ratio * 900
    score += min(total, 8) * 75
    score -= unknown_count * 140
    if row["queue_bucket"].startswith("all_mature"):
        score += 400
    if row["queue_bucket"] == "long_text_composer_blocker":
        score += 160
    if row["queue_bucket"] == "autofix_unknown_blocker":
        score += 80
    score += int(row.get("segment_id") or 0) % 23 / 100
    return round(score, 4)


def suggested_decision(row: dict[str, Any]) -> str:
    evidence = parse_json_dict(row.get("evidence_json"))
    composition = evidence.get("_multiagent_composition", {})
    if int(composition.get("unknown_family_count") or 0) > 0:
        return "needs_new_microagent"
    return "needs_domain_context"


def fetch_candidates(
    conn,
    *,
    partial_run: dict[str, Any],
    agent_key: str,
    min_issues: int,
    min_mature_families: int,
    min_mature_ratio: float,
    include_existing: bool,
) -> list[dict[str, Any]]:
    partial_run_id = int(partial_run["id"])
    ledger_run_id = int(partial_run["ledger_run_id"])
    maturity = family_maturity(conn, partial_run_id)
    existing_segments = existing_composition_segments(conn, agent_key=agent_key, include_existing=include_existing)
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_partial_coverage_items
        WHERE run_id = ?
          AND coverage_state <> 'full'
          AND total_issue_count >= ?
        ORDER BY total_issue_count DESC, relative_path, source_key
        """,
        (partial_run_id, min_issues),
    ).fetchall()
    segment_payloads: list[dict[str, Any]] = []
    segment_ids: set[int] = set()
    for row in rows:
        payload = dict(row)
        segment_id = int(payload["segment_id"])
        if segment_id in existing_segments:
            continue
        open_families = parse_json_dict(payload.get("open_families_json"))
        if not open_families:
            continue
        family_count = len(open_families)
        mature_families = [
            family for family in open_families if maturity.get(family, {}).get("has_checkpoint_evidence")
        ]
        unknown_families = [family for family in open_families if family not in mature_families]
        mature_family_count = len(mature_families)
        issue_mature_count = sum(int(open_families.get(family) or 0) for family in mature_families)
        open_issue_count = int(payload.get("open_issue_count") or 0)
        mature_ratio = mature_family_count / family_count if family_count else 0.0
        issue_mature_ratio = issue_mature_count / open_issue_count if open_issue_count else 0.0
        if mature_family_count < min_mature_families:
            continue
        if mature_ratio < min_mature_ratio:
            continue
        cluster = segment_cluster(str(payload.get("relative_path") or ""), open_families)
        payload["_composition"] = {
            "partial_coverage_run_id": partial_run_id,
            "ledger_run_id": ledger_run_id,
            "segment_state_run_id": int(partial_run["segment_state_run_id"]),
            "total_issue_count": int(payload.get("total_issue_count") or 0),
            "open_issue_count": open_issue_count,
            "family_count": family_count,
            "mature_family_count": mature_family_count,
            "unknown_family_count": len(unknown_families),
            "mature_family_ratio": round(mature_ratio, 4),
            "issue_mature_ratio": round(issue_mature_ratio, 4),
            "mature_families": mature_families,
            "unknown_families": unknown_families,
            "open_families": open_families,
            "covered_families": parse_json_dict(payload.get("covered_families_json")),
            "cluster": cluster,
        }
        segment_payloads.append(payload)
        segment_ids.add(segment_id)

    ledger_by_segment = load_segment_ledger_items(conn, ledger_run_id=ledger_run_id, segment_ids=segment_ids)
    candidates: list[dict[str, Any]] = []
    for segment in segment_payloads:
        composition = segment["_composition"]
        ledger_items = ledger_by_segment.get(int(segment["segment_id"]), [])
        selected_item = chosen_ledger_item(
            ledger_items,
            unknown_families=composition["unknown_families"],
            mature_families=composition["mature_families"],
        )
        if selected_item is None:
            continue
        evidence = parse_json_dict(selected_item.get("evidence_json"))
        evidence["_multiagent_composition"] = composition
        evidence["_selected_issue"] = {
            "original_agent_key": selected_item.get("agent_key"),
            "issue_family": selected_item.get("issue_family"),
            "issue_kind": selected_item.get("issue_kind"),
            "reason": "unknown_family_blocker" if composition["unknown_families"] else "all_families_have_checkpoint_evidence",
        }
        row = dict(selected_item)
        row["agent_key"] = agent_key
        row["queue_bucket"] = opportunity_bucket(
            total_issues=int(composition["total_issue_count"]),
            family_count=int(composition["family_count"]),
            mature_family_count=int(composition["mature_family_count"]),
            unknown_families=list(composition["unknown_families"]),
            cluster=str(composition["cluster"]),
        )
        row["evidence_json"] = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        row["evidence_text"] = (
            f"Multiagent composition candidate: {composition['mature_family_count']}/"
            f"{composition['family_count']} mature families; open={composition['open_issue_count']}; "
            f"cluster={composition['cluster']}; selected={selected_item.get('issue_family')}/"
            f"{selected_item.get('issue_kind')}. Original evidence: {short(selected_item.get('evidence_text'))}"
        )
        row["priority_score"] = composition_priority(row)
        row["suggested_decision"] = suggested_decision(row)
        candidates.append(row)
    candidates.sort(
        key=lambda row: (
            -float(row["priority_score"]),
            row["queue_bucket"],
            row["relative_path"],
            row["source_key"],
        )
    )
    return candidates


def select_rows(candidates: list[dict[str, Any]], *, limit: int, per_bucket: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        grouped[row["queue_bucket"]].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: (-float(row["priority_score"]), row["relative_path"], row["source_key"]))
    bucket_order = [
        "all_mature_2_issue",
        "all_mature_3_4_issue",
        "all_mature_high_issue",
        "long_text_composer_blocker",
        "autofix_unknown_blocker",
        "nickname_multiagent_cluster",
        "activity_multiagent_cluster",
        "high_maturity_mixed_cluster",
        "mixed_near_closure",
    ]
    bucket_order.extend(sorted(bucket for bucket in grouped if bucket not in set(bucket_order)))
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    for index in range(per_bucket):
        for bucket in bucket_order:
            rows = grouped.get(bucket, [])
            if index >= len(rows):
                continue
            if len(selected) >= limit:
                break
            row = rows[index]
            selected.append(row)
            selected_ids.add(int(row["segment_id"]))
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for row in candidates:
            if len(selected) >= limit:
                break
            segment_id = int(row["segment_id"])
            if segment_id in selected_ids:
                continue
            selected.append(row)
            selected_ids.add(segment_id)
    return selected


def insert_queue_run(
    conn,
    *,
    partial_run: dict[str, Any],
    agent_key: str,
    limit: int,
    per_bucket: int,
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    return int(
        conn.execute(
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
                int(partial_run["ledger_run_id"]),
                agent_key,
                "multi_issue_composition",
                "multi_issue_composition_opportunity",
                limit,
                per_bucket,
                len(selected),
                len(selected),
                json.dumps(dict(bucket_counts.most_common()), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                str(decisions_template_path),
                now,
                now,
                now,
            ),
        ).lastrowid
    )


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    partial_run: dict[str, Any],
    agent_key: str,
    candidates_count: int,
    rows: list[dict[str, Any]],
    limit: int,
    per_bucket: int,
    min_issues: int,
    min_mature_families: int,
    min_mature_ratio: float,
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(row["queue_bucket"] for row in rows)
    cluster_counts: Counter[str] = Counter()
    mature_ratio_counts: Counter[str] = Counter()
    selected_family_counts: Counter[str] = Counter()
    for row in rows:
        evidence = parse_json_dict(row.get("evidence_json"))
        composition = evidence.get("_multiagent_composition", {})
        cluster_counts[str(composition.get("cluster") or "unknown")] += 1
        selected_family_counts[str(row.get("issue_family") or "unknown")] += 1
        ratio_value = float(composition.get("mature_family_ratio") or 0.0)
        if ratio_value >= 1:
            mature_ratio_counts["100%"] += 1
        elif ratio_value >= 0.75:
            mature_ratio_counts["75-99%"] += 1
        elif ratio_value >= 0.5:
            mature_ratio_counts["50-74%"] += 1
        else:
            mature_ratio_counts["below_50%"] += 1

    fieldnames = [
        "queue_run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "priority_score",
        "suggested_decision",
        "selected_issue_family",
        "selected_issue_kind",
        "original_agent_key",
        "total_issue_count",
        "open_issue_count",
        "family_count",
        "mature_family_count",
        "unknown_family_count",
        "mature_family_ratio",
        "issue_mature_ratio",
        "cluster",
        "open_families_json",
        "mature_families_json",
        "unknown_families_json",
        "evidence_text",
        "english_text",
        "spanish_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            evidence = parse_json_dict(row.get("evidence_json"))
            composition = evidence.get("_multiagent_composition", {})
            selected_issue = evidence.get("_selected_issue", {})
            writer.writerow(
                {
                    "queue_run_id": queue_run_id,
                    "ledger_item_id": row["id"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "source_line_number": row["source_line_number"],
                    "queue_bucket": row["queue_bucket"],
                    "priority_score": row["priority_score"],
                    "suggested_decision": row["suggested_decision"],
                    "selected_issue_family": row["issue_family"],
                    "selected_issue_kind": row["issue_kind"],
                    "original_agent_key": selected_issue.get("original_agent_key"),
                    "total_issue_count": composition.get("total_issue_count"),
                    "open_issue_count": composition.get("open_issue_count"),
                    "family_count": composition.get("family_count"),
                    "mature_family_count": composition.get("mature_family_count"),
                    "unknown_family_count": composition.get("unknown_family_count"),
                    "mature_family_ratio": composition.get("mature_family_ratio"),
                    "issue_mature_ratio": composition.get("issue_mature_ratio"),
                    "cluster": composition.get("cluster"),
                    "open_families_json": json.dumps(composition.get("open_families") or {}, ensure_ascii=False, sort_keys=True),
                    "mature_families_json": json.dumps(composition.get("mature_families") or [], ensure_ascii=False),
                    "unknown_families_json": json.dumps(composition.get("unknown_families") or [], ensure_ascii=False),
                    "evidence_text": row.get("evidence_text"),
                    "english_text": row.get("english_text"),
                    "spanish_text": row.get("spanish_text"),
                    "confirmed_text": row.get("confirmed_text"),
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_run_id": queue_run_id,
                "ledger_run_id": int(partial_run["ledger_run_id"]),
                "partial_coverage_run_id": int(partial_run["id"]),
                "ledger_item_id": row["id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "agent_key": agent_key,
                "queue_bucket": row["queue_bucket"],
                "priority_score": row["priority_score"],
                "suggested_decision": row["suggested_decision"],
                "issue_family": row["issue_family"],
                "issue_kind": row["issue_kind"],
                "evidence": parse_json_dict(row.get("evidence_json")),
                "texts": {
                    "english_text": row.get("english_text"),
                    "spanish_text": row.get("spanish_text"),
                    "confirmed_text": row.get("confirmed_text"),
                    "evidence_text": row.get("evidence_text"),
                },
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        decision_options = [
            "composition_ready",
            "needs_repair",
            "needs_domain_context",
            "needs_new_microagent",
            "manual_exception",
            "false_positive_reopen",
        ]
        for row in rows:
            payload = {
                "queue_run_id": queue_run_id,
                "ledger_item_id": row["id"],
                "segment_id": row["segment_id"],
                "decision": "",
                "decision_options": decision_options,
                "corrected_text": "",
                "notes": "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue multiagent composition queue",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Partial coverage run id: {partial_run['id']}",
        f"Ledger run id: {partial_run['ledger_run_id']}",
        f"Segment-state run id: {partial_run['segment_state_run_id']}",
        f"Agent: {agent_key}",
        "",
        "Selection:",
        f"- Candidates available: {candidates_count:,}",
        f"- Selected: {len(rows):,} ({percent(len(rows), candidates_count):.2f}%)",
        f"- Limit: {limit:,}",
        f"- Per bucket: {per_bucket:,}",
        f"- Min issues: {min_issues:,}",
        f"- Min mature families: {min_mature_families:,}",
        f"- Min mature family ratio: {min_mature_ratio:.2f}",
        "",
        "Source coverage context:",
        f"- Total segments with issues: {int(partial_run['total_segments_with_issues'] or 0):,}",
        f"- Total issue items: {int(partial_run['total_issue_items'] or 0):,}",
        f"- Covered issue items: {int(partial_run['covered_issue_items'] or 0):,}",
        f"- Fully covered segments: {int(partial_run['fully_covered_segments'] or 0):,}",
        f"- Partially covered segments: {int(partial_run['partially_covered_segments'] or 0):,}",
        f"- Uncovered segments: {int(partial_run['uncovered_segments'] or 0):,}",
        "",
        "Buckets:",
        *[f"- {bucket}: {count:,}" for bucket, count in bucket_counts.most_common()],
        "",
        "Clusters:",
        *[f"- {cluster}: {count:,}" for cluster, count in cluster_counts.most_common()],
        "",
        "Mature family ratio:",
        *[f"- {bucket}: {count:,}" for bucket, count in mature_ratio_counts.most_common()],
        "",
        "Selected issue families:",
        *[f"- {family}: {count:,}" for family, count in selected_family_counts.most_common(12)],
        "",
        "Review guidance:",
        "- Treat each row as a segment-level coordination candidate, not as a single-neuron verdict.",
        "- `composition_ready` means all visible issue families appear covered by existing neurons and the row should seed a composition checkpoint.",
        "- `needs_new_microagent` means one missing family blocks reuse across many similar segments.",
        "- `needs_repair` means a specific visible repair is needed before composition can close the segment.",
        "- Do not apply output from this queue; this is learning-front evidence only.",
        "",
        "Files:",
        f"- CSV: {csv_path}",
        f"- JSONL: {jsonl_path}",
        f"- Decisions template: {decisions_template_path}",
        "",
        "Samples:",
    ]
    for row in rows[:30]:
        evidence = parse_json_dict(row.get("evidence_json"))
        composition = evidence.get("_multiagent_composition", {})
        lines.append(
            (
                f"- ledger {row['id']} | segment {row['segment_id']} | {row['queue_bucket']} | "
                f"mature={composition.get('mature_family_count')}/{composition.get('family_count')} | "
                f"issues={composition.get('open_issue_count')} | {row['relative_path']}::{row['source_key']}"
            )
        )
        lines.append(f"  open_families: {json.dumps(composition.get('open_families') or {}, ensure_ascii=False, sort_keys=True)}")
        lines.append(f"  unknown_families: {json.dumps(composition.get('unknown_families') or [], ensure_ascii=False)}")
        lines.append(f"  selected_issue: {row['issue_family']} / {row['issue_kind']}")
        lines.append(f"  evidence: {short(row.get('evidence_text'))}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    partial_run_id: int | None = None,
    agent_key: str = DEFAULT_AGENT_KEY,
    limit: int | None = None,
    per_bucket: int = DEFAULT_PER_BUCKET,
    min_issues: int = 2,
    min_mature_families: int = 2,
    min_mature_ratio: float = 0.75,
    include_existing: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    selected_limit = limit or DEFAULT_LIMIT
    paths = report_paths(settings)
    print("[issue_multiagent_composition_queue] Starting multiagent composition queue")
    print(f"[issue_multiagent_composition_queue] Rule version: {RULE_VERSION}")
    print(f"[issue_multiagent_composition_queue] Agent: {agent_key}")
    print(f"[issue_multiagent_composition_queue] Limit: {selected_limit}")
    print(f"[issue_multiagent_composition_queue] Per bucket: {per_bucket}")
    print(f"[issue_multiagent_composition_queue] Min issues: {min_issues}")
    print(f"[issue_multiagent_composition_queue] Min mature families: {min_mature_families}")
    print(f"[issue_multiagent_composition_queue] Min mature ratio: {min_mature_ratio:.2f}")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        partial_run = latest_partial_coverage_run(conn, partial_run_id)
        candidates = fetch_candidates(
            conn,
            partial_run=partial_run,
            agent_key=agent_key,
            min_issues=min_issues,
            min_mature_families=min_mature_families,
            min_mature_ratio=min_mature_ratio,
            include_existing=include_existing,
        )
        selected = select_rows(candidates, limit=selected_limit, per_bucket=per_bucket)
        queue_run_id = insert_queue_run(
            conn,
            partial_run=partial_run,
            agent_key=agent_key,
            limit=selected_limit,
            per_bucket=per_bucket,
            selected=selected,
            paths=paths,
        )
        issue_review_queue.insert_queue_items(
            conn,
            queue_run_id,
            int(partial_run["ledger_run_id"]),
            selected,
        )
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        partial_run=partial_run,
        agent_key=agent_key,
        candidates_count=len(candidates),
        rows=selected,
        limit=selected_limit,
        per_bucket=per_bucket,
        min_issues=min_issues,
        min_mature_families=min_mature_families,
        min_mature_ratio=min_mature_ratio,
    )
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    print(f"[issue_multiagent_composition_queue] Queue run id: {queue_run_id}")
    print(f"[issue_multiagent_composition_queue] Partial coverage run id: {partial_run['id']}")
    print(f"[issue_multiagent_composition_queue] Candidates: {len(candidates)}")
    print(f"[issue_multiagent_composition_queue] Selected: {len(selected)}")
    print(f"[issue_multiagent_composition_queue] Report: {txt_path}")
    print(f"[issue_multiagent_composition_queue] CSV: {csv_path}")
    print(f"[issue_multiagent_composition_queue] JSONL: {jsonl_path}")
    print(f"[issue_multiagent_composition_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "partial_coverage_run_id": int(partial_run["id"]),
        "ledger_run_id": int(partial_run["ledger_run_id"]),
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a segment-level multiagent composition opportunity queue.")
    parser.add_argument("--partial-run-id", type=int, default=None)
    parser.add_argument("--agent-key", default=DEFAULT_AGENT_KEY)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--per-bucket", type=int, default=DEFAULT_PER_BUCKET)
    parser.add_argument("--min-issues", type=int, default=2)
    parser.add_argument("--min-mature-families", type=int, default=2)
    parser.add_argument("--min-mature-ratio", type=float, default=0.75)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        partial_run_id=args.partial_run_id,
        agent_key=args.agent_key,
        limit=args.limit,
        per_bucket=args.per_bucket,
        min_issues=args.min_issues,
        min_mature_families=args.min_mature_families,
        min_mature_ratio=args.min_mature_ratio,
        include_existing=args.include_existing,
    )
