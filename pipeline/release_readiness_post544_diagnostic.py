from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_post544_diagnostic_v1"
DEFAULT_RUN_ID = 544
RESIDUAL_JSONL = Path("reports/20260702_124918_915442_domain_policy_vote_candidate_post544_residual_blocks_diagnostic.jsonl")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only release readiness diagnostic after current run.")
    parser.add_argument("--run-id", type=int, default=DEFAULT_RUN_ID)
    parser.add_argument("--residual-jsonl", type=Path, default=RESIDUAL_JSONL)
    return parser.parse_args()


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    full = db.project_path(path)
    if not full.exists():
        return []
    rows: list[dict[str, Any]] = []
    with full.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def token_surface(text: str | None) -> str:
    text = text or ""
    tokens = protected_tokens(text)
    if not tokens:
        return "plain_text"
    blob = " ".join(tokens)
    if "\\n" in text or "\n" in text:
        return "multiline"
    if "Select_CString" in blob or "SelectLocalization" in blob:
        return "dynamic_select"
    if any(part in blob for part in [".Get", ".Custom", "ROOT.", "scope:", "SCOPE.", "GetScriptValue"]):
        return "dynamic_getter"
    return "light_token"


def visibility_group(relative_path: str, source_key: str) -> str:
    path = relative_path.lower()
    key = source_key.lower()
    if "event_localization" in path or "events" in path:
        return "narrative_events"
    if "interface" in path or "gui" in path or key.endswith("_tt") or "tooltip" in key or ".tt" in key:
        return "ui_tooltips_short_labels"
    if "religion" in path or "faith" in key or "doctrine" in key or "holy_site" in key:
        return "religion_faith_doctrine"
    if "culture" in path or "tradition" in key or "innovation" in key:
        return "culture_tradition_innovation"
    if "title" in path or "realm" in key or "landed" in key or "governance" in key:
        return "title_realm_governance"
    if "nickname" in path or "nick" in key:
        return "nicknames"
    if "activities" in path or "activity" in key:
        return "activities"
    if "interaction" in path or "scheme" in path or "contract" in path:
        return "interactions_schemes_contracts"
    return "other_visible_or_system"


def issue_summary(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    output: dict[int, dict[str, Any]] = {}
    for offset in range(0, len(segment_ids), 800):
        chunk = segment_ids[offset : offset + 800]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT
              segment_id,
              COUNT(*) AS issue_count,
              SUM(CASE WHEN lower(COALESCE(issue_severity,'')) IN ('high','error','critical') THEN 1 ELSE 0 END) AS high_issue_count,
              GROUP_CONCAT(DISTINCT issue_family) AS issue_families,
              GROUP_CONCAT(DISTINCT issue_kind) AS issue_kinds,
              GROUP_CONCAT(DISTINCT agent_key) AS agent_keys
            FROM ml_issue_ledger_items
            WHERE segment_id IN ({placeholders})
              AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
            GROUP BY segment_id
            """,
            chunk,
        ).fetchall()
        for row in rows:
            output[int(row["segment_id"])] = {
                "open_issue_count": int(row["issue_count"] or 0),
                "high_issue_count": int(row["high_issue_count"] or 0),
                "issue_families": row["issue_families"] or "",
                "issue_kinds": row["issue_kinds"] or "",
                "agent_keys": row["agent_keys"] or "",
            }
    return output


def fetch_pending_rows(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          state.segment_id,
          state.relative_path,
          state.source_key,
          state.source_line_number,
          state.final_state,
          state.state_group,
          state.output_state,
          state.review_state,
          state.apply_state,
          state.confirmation_level,
          state.confirmation_label,
          state.locked,
          state.confirmed_matches_output,
          state.needs_output_apply,
          state.needs_reopen,
          state.is_closed,
          state.lifecycle_policy_action,
          state.lifecycle_policy_allowed,
          src.spanish_text,
          src.english_text,
          output.portuguese_text AS output_text,
          conf.confirmed_text,
          conf.confirmation_source
        FROM segment_state_items state
        JOIN source_segments src ON src.id = state.segment_id
        LEFT JOIN output_segments output ON output.segment_id = state.segment_id
        LEFT JOIN segment_confirmations conf ON conf.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.is_closed = 0
        ORDER BY state.priority_score DESC, state.segment_id
        """,
        (run_id,),
    ).fetchall()
    output = [dict(row) for row in rows]
    issues = issue_summary(conn, [int(row["segment_id"]) for row in output])
    for row in output:
        row.update(
            issues.get(
                int(row["segment_id"]),
                {"open_issue_count": 0, "high_issue_count": 0, "issue_families": "", "issue_kinds": "", "agent_keys": ""},
            )
        )
    return output


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part for part in str(value).split(",") if part]


def has_spanish_residue(row: dict[str, Any]) -> bool:
    blob = " ".join(
        [
            str(row.get("issue_families") or ""),
            str(row.get("issue_kinds") or ""),
            str(row.get("output_text") or ""),
            str(row.get("confirmed_text") or ""),
        ]
    ).lower()
    markers = [
        "spanish",
        "residual",
        "residue",
        " el ",
        " la ",
        " los ",
        " las ",
        " de la ",
        " del ",
        " señor",
        " rey ",
        " reino ",
        " con ",
        " para ",
        " que ",
    ]
    return "spanish_residual" in blob or "spanish_residue" in blob or any(marker in blob for marker in markers)


def has_gender_perspective(row: dict[str, Any]) -> bool:
    blob = " ".join(
        [
            str(row.get("issue_families") or ""),
            str(row.get("issue_kinds") or ""),
            str(row.get("output_text") or ""),
            str(row.get("confirmed_text") or ""),
        ]
    )
    return any(marker in blob for marker in ["gender", "ES_", "ES_OA", "Select_CString", "perspective", "RelationToMe", "GetWomanMan"])


def release_class(row: dict[str, Any]) -> str:
    surface = row["token_surface"]
    visibility = row["visibility_group"]
    high_issue = int(row.get("high_issue_count") or 0) > 0
    open_issue = int(row.get("open_issue_count") or 0) > 0
    needs_apply = int(row.get("needs_output_apply") or 0) == 1
    confirmed_mismatch = int(row.get("confirmed_matches_output") or 0) == 0
    visible_spanish = bool(row.get("spanish_residue_visible"))
    gender = bool(row.get("gender_or_perspective"))

    if needs_apply or confirmed_mismatch:
        return "release_blocker"
    if high_issue:
        return "release_blocker" if visibility in {"narrative_events", "ui_tooltips_short_labels", "religion_faith_doctrine"} else "review_before_release"
    if visible_spanish and visibility in {"ui_tooltips_short_labels", "narrative_events", "religion_faith_doctrine", "culture_tradition_innovation"}:
        return "review_before_release"
    if open_issue and surface in {"plain_text", "light_token"} and visibility in {"ui_tooltips_short_labels", "narrative_events"}:
        return "review_before_release"
    if gender and visibility in {"ui_tooltips_short_labels", "narrative_events"}:
        return "review_before_release"
    if surface in {"dynamic_getter", "dynamic_select", "multiline"}:
        return "parser_later"
    return "known_non_blocking_hold"


def impact_score(row: dict[str, Any]) -> int:
    score = 0
    if row["release_class"] == "release_blocker":
        score += 100
    elif row["release_class"] == "review_before_release":
        score += 60
    elif row["release_class"] == "parser_later":
        score += 20
    visibility_weight = {
        "ui_tooltips_short_labels": 35,
        "narrative_events": 30,
        "religion_faith_doctrine": 25,
        "culture_tradition_innovation": 22,
        "title_realm_governance": 22,
        "nicknames": 20,
        "activities": 18,
        "interactions_schemes_contracts": 16,
    }
    score += visibility_weight.get(row["visibility_group"], 8)
    score += min(int(row.get("open_issue_count") or 0), 5) * 3
    score += min(int(row.get("high_issue_count") or 0), 3) * 20
    if row["spanish_residue_visible"]:
        score += 20
    if row["gender_or_perspective"]:
        score += 10
    return score


def compact_sample(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_id": row.get("segment_id"),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "final_state": row.get("final_state"),
        "release_class": row.get("release_class"),
        "visibility_group": row.get("visibility_group"),
        "token_surface": row.get("token_surface"),
        "open_issue_count": row.get("open_issue_count"),
        "high_issue_count": row.get("high_issue_count"),
        "issue_families": row.get("issue_families"),
        "issue_kinds": row.get("issue_kinds"),
        "needs_output_apply": row.get("needs_output_apply"),
        "confirmed_matches_output": row.get("confirmed_matches_output"),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
    }


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    residual_rows = read_jsonl(args.residual_jsonl)
    residual_by_id = {int(row["segment_id"]): row for row in residual_rows if row.get("segment_id")}
    with connect_readonly() as conn:
        pending_rows = fetch_pending_rows(conn, args.run_id)
        run = conn.execute("SELECT * FROM segment_state_runs WHERE id=?", (args.run_id,)).fetchone()
        if run is None:
            raise SystemExit(f"missing segment_state_run {args.run_id}")
        run_dict = dict(run)

    records: list[dict[str, Any]] = []
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in pending_rows:
        output_text = row.get("output_text") or ""
        confirmed_text = row.get("confirmed_text") or ""
        surface = token_surface(confirmed_text or output_text)
        visibility = visibility_group(str(row.get("relative_path") or ""), str(row.get("source_key") or ""))
        canonical_equal = canonical_localization_text(output_text) == canonical_localization_text(confirmed_text)
        record = {
            "source": SOURCE,
            "record_type": "release_readiness_pending_row",
            **row,
            "token_surface": surface,
            "visibility_group": visibility,
            "canonical_output_equals_confirmed": canonical_equal,
            "output_differs_from_confirmed": not canonical_equal,
            "spanish_residue_visible": has_spanish_residue(row),
            "gender_or_perspective": has_gender_perspective(row),
            "residual_record_type": residual_by_id.get(int(row["segment_id"]), {}).get("record_type"),
            "residual_diagnostic_bucket": residual_by_id.get(int(row["segment_id"]), {}).get("diagnostic_bucket"),
        }
        record["release_class"] = release_class(record)
        record["impact_score"] = impact_score(record)
        records.append(record)
        sample_key = f"{record['release_class']}::{record['visibility_group']}::{record['token_surface']}"
        if len(samples[sample_key]) < 5:
            samples[sample_key].append(compact_sample(record))

    release_counts = Counter(record["release_class"] for record in records)
    class_visibility_counts = Counter(f"{record['release_class']}::{record['visibility_group']}" for record in records)
    visibility_counts = Counter(record["visibility_group"] for record in records)
    surface_counts = Counter(record["token_surface"] for record in records)
    final_state_counts = Counter(str(record["final_state"]) for record in records)
    issue_family_counts: Counter[str] = Counter()
    high_issue_family_counts: Counter[str] = Counter()
    for record in records:
        families = split_csv(record.get("issue_families")) or ["none"]
        issue_family_counts.update(families)
        if int(record.get("high_issue_count") or 0) > 0:
            high_issue_family_counts.update(families)

    group_stats: dict[str, dict[str, Any]] = {}
    for key in class_visibility_counts:
        cls, visibility = key.split("::", 1)
        group_records = [record for record in records if record["release_class"] == cls and record["visibility_group"] == visibility]
        group_stats[key] = {
            "release_class": cls,
            "visibility_group": visibility,
            "count": len(group_records),
            "avg_impact_score": round(sum(record["impact_score"] for record in group_records) / len(group_records), 2),
            "token_surface_counts": dict(Counter(record["token_surface"] for record in group_records).most_common()),
            "top_issue_families": dict(
                Counter(
                    family
                    for record in group_records
                    for family in (split_csv(record.get("issue_families")) or ["none"])
                ).most_common(8)
            ),
        }
    top_groups = sorted(group_stats.values(), key=lambda item: (item["avg_impact_score"], item["count"]), reverse=True)[:10]

    debug_non_plain = [
        record
        for record in records
        if residual_by_id.get(int(record["segment_id"]), {}).get("record_type") == "debug_existing_policy_consumption"
        and record["token_surface"] != "plain_text"
    ]
    phase3_dynamic = [
        record
        for record in records
        if residual_by_id.get(int(record["segment_id"]), {}).get("record_type")
        == "phase3_human_misc_equal_output_remaining"
    ]
    needs_output_apply = [record for record in records if int(record.get("needs_output_apply") or 0) == 1]
    output_mismatch = [record for record in records if record["output_differs_from_confirmed"]]

    final_package_recommendations = [
        {
            "rank": 1,
            "package": "release_blocker_pending_apply_confirmed",
            "count": len(needs_output_apply),
            "mode": "hold_or_protected_apply_only_with_explicit_token_policy",
            "reason": "Only true output/confirmation mismatch path; current item is token-dynamic and should not be applied automatically.",
        },
        {
            "rank": 2,
            "package": "review_before_release_ui_tooltips_short_labels_plain_light",
            "count": sum(
                1
                for record in records
                if record["release_class"] == "review_before_release"
                and record["visibility_group"] == "ui_tooltips_short_labels"
                and record["token_surface"] in {"plain_text", "light_token"}
            ),
            "mode": "small_human_review_or_issue_triage",
            "reason": "Most visible compact UI text; likely highest player-facing polish return.",
        },
        {
            "rank": 3,
            "package": "review_before_release_narrative_events_spanish_or_gender",
            "count": sum(
                1
                for record in records
                if record["release_class"] == "review_before_release"
                and record["visibility_group"] == "narrative_events"
                and (record["spanish_residue_visible"] or record["gender_or_perspective"])
            ),
            "mode": "small_human_review_packet",
            "reason": "Visible in gameplay prose, but many dynamic/multiline cases should be sampled rather than auto-fixed.",
        },
        {
            "rank": 4,
            "package": "parser_later_dynamic_getter_select_multiline",
            "count": sum(1 for record in records if record["release_class"] == "parser_later"),
            "mode": "hold_for_parser_policy",
            "reason": "Large volume but not a release blocker if output is present and no confirmed mismatch.",
        },
    ]

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_release_readiness_diagnostic",
        "run_id": args.run_id,
        "run": {
            "closed_count": int(run_dict.get("closed_count") or 0),
            "pending_count": int(run_dict.get("pending_count") or 0),
            "reopen_count": int(run_dict.get("reopen_count") or 0),
            "output_apply_pending_count": int(run_dict.get("output_apply_pending_count") or 0),
            "total_segments": int(run_dict.get("total_segments") or 0),
        },
        "pending_global_count": len(records),
        "release_class_counts": dict(release_counts.most_common()),
        "release_blocker_count": release_counts.get("release_blocker", 0),
        "review_before_release_count": release_counts.get("review_before_release", 0),
        "known_non_blocking_hold_count": release_counts.get("known_non_blocking_hold", 0),
        "parser_later_count": release_counts.get("parser_later", 0),
        "needs_output_apply_count": len(needs_output_apply),
        "needs_output_apply_segment_ids": [int(record["segment_id"]) for record in needs_output_apply],
        "output_differs_from_confirmed_count": len(output_mismatch),
        "output_differs_from_confirmed_segment_ids": [int(record["segment_id"]) for record in output_mismatch[:50]],
        "open_issue_pending_count": sum(1 for record in records if int(record.get("open_issue_count") or 0) > 0),
        "high_issue_pending_count": sum(1 for record in records if int(record.get("high_issue_count") or 0) > 0),
        "spanish_residue_visible_count": sum(1 for record in records if record["spanish_residue_visible"]),
        "gender_or_perspective_count": sum(1 for record in records if record["gender_or_perspective"]),
        "visibility_group_counts": dict(visibility_counts.most_common()),
        "token_surface_counts": dict(surface_counts.most_common()),
        "final_state_counts": dict(final_state_counts.most_common(30)),
        "issue_family_counts": dict(issue_family_counts.most_common(40)),
        "high_issue_family_counts": dict(high_issue_family_counts.most_common(40)),
        "top_10_release_impact_groups": top_groups,
        "debug_non_plain_count": len(debug_non_plain),
        "debug_non_plain_token_surface_counts": dict(Counter(record["token_surface"] for record in debug_non_plain).most_common()),
        "debug_non_plain_release_class_counts": dict(Counter(record["release_class"] for record in debug_non_plain).most_common()),
        "phase3_dynamic_remaining_count": len(phase3_dynamic),
        "phase3_dynamic_token_surface_counts": dict(Counter(record["token_surface"] for record in phase3_dynamic).most_common()),
        "final_package_recommendations": final_package_recommendations,
        "samples_by_group": dict(samples),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "For release readiness, do not chase total closure. Treat the one needs_output_apply item as hold/token-dynamic unless explicit policy is approved; "
            "run small human review packets only for review_before_release UI/tooltip and visible narrative Spanish/gender risks. "
            "Leave the 93 debug non-plain as technical hold/parser-later unless a parser policy is being designed."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_post544_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Release readiness post-544 diagnostic",
        f"run_id={summary['run_id']}",
        f"pending_global_count={summary['pending_global_count']}",
        f"release_class_counts={json.dumps(summary['release_class_counts'], ensure_ascii=False, sort_keys=True)}",
        f"needs_output_apply_count={summary['needs_output_apply_count']}",
        f"output_differs_from_confirmed_count={summary['output_differs_from_confirmed_count']}",
        f"open_issue_pending_count={summary['open_issue_pending_count']}",
        f"high_issue_pending_count={summary['high_issue_pending_count']}",
        f"spanish_residue_visible_count={summary['spanish_residue_visible_count']}",
        f"gender_or_perspective_count={summary['gender_or_perspective_count']}",
        f"debug_non_plain_count={summary['debug_non_plain_count']}",
        "",
        "Top 10 release impact groups:",
        json.dumps(summary["top_10_release_impact_groups"], ensure_ascii=False, indent=2, sort_keys=True),
        "",
        "Final package recommendations:",
        json.dumps(summary["final_package_recommendations"], ensure_ascii=False, indent=2, sort_keys=True),
        "",
        "Recommendation:",
        summary["single_operational_recommendation"],
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args)
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"pending_global_count={summary['pending_global_count']}")
    print(f"release_blocker_count={summary['release_blocker_count']}")
    print(f"review_before_release_count={summary['review_before_release_count']}")
    print(f"known_non_blocking_hold_count={summary['known_non_blocking_hold_count']}")
    print(f"parser_later_count={summary['parser_later_count']}")
    print(f"debug_non_plain_count={summary['debug_non_plain_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
