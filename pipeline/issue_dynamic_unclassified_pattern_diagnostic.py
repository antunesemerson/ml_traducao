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
from issue_review_assisted_draft import has_actual_mojibake, spanish_hits


RULE_VERSION = "issue_dynamic_unclassified_pattern_diagnostic_v1"
DIAGNOSTIC_NAME = "dynamic_ck3_unclassified_pattern_router_v1"
TARGET_BLOCKER = "unclassified_dynamic_ck3_pattern"

SPANISH_LITERAL_RE = re.compile(
    r"\b("
    r"ganaste|gan[oó]|ganar[aá]|perder[aá]|compartisteis|compartieron|"
    r"obligaste|oblig[oó]|cedas|robarme|ladrona|ladr[oó]n|"
    r"gobernadora|gobernador|se[nñ]ora|se[nñ]or|nuestra|nuestro|"
    r"de la|del sanador|sanadora|maravilloso|cumple|requisitos"
    r")\b",
    re.IGNORECASE,
)

TOKEN_FEATURES: tuple[tuple[str, str], ...] = (
    ("script_value", "ScriptValue"),
    ("get_activity_type", "GetActivityType"),
    ("get_trait", "GetTrait"),
    ("get_maa", "GetMaA"),
    ("get_scheme", "GetScheme"),
    ("get_law", "GetLaw"),
    ("get_title_by_key", "GetTitleByKey"),
    ("get_short_ui_name", "GetShortUIName"),
    ("local_player_string", "LocalPlayerString"),
    ("custom_loc", "Custom("),
    ("select_cstring", "Select_CString"),
    ("concept", "Concept("),
)


def parse_ids(value: str | None) -> list[int]:
    if not value:
        return []
    ids: list[int] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if chunk:
            ids.append(int(chunk))
    return ids


def short(value: str | None, limit: int = 220) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_dynamic_unclassified_pattern_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".json")


def latest_shadow_run_ids(conn, limit: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT id
        FROM ml_issue_dynamic_ck3_pattern_shadow_runs
        WHERE finished_at IS NOT NULL
          AND blocked_count > 0
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def latest_segment_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished segment_state_runs found.")
    return int(row["id"])


def latest_partial_coverage_run_id(conn) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_partial_coverage_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_dynamic_unclassified_pattern_diagnostic_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            diagnostic_name TEXT NOT NULL,
            shadow_run_ids_json TEXT NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            partial_coverage_run_id INTEGER,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            route_lane_counts_json TEXT,
            token_signature_counts_json TEXT,
            path_group_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            json_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_dynamic_unclassified_pattern_diagnostic_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            shadow_item_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            path_group TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            issue_kind TEXT NOT NULL,
            queue_bucket TEXT,
            route_lane TEXT NOT NULL,
            route_reason TEXT NOT NULL,
            token_signature TEXT NOT NULL,
            risk_flags_json TEXT,
            coverage_state TEXT,
            total_issue_count INTEGER,
            open_issue_count INTEGER,
            blocked_issue_count INTEGER,
            text_sample TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_dynamic_unclassified_pattern_diagnostic_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_dynamic_unclassified_diag_items_run
        ON ml_issue_dynamic_unclassified_pattern_diagnostic_items(run_id, route_lane, token_signature);

        CREATE INDEX IF NOT EXISTS idx_dynamic_unclassified_diag_items_segment
        ON ml_issue_dynamic_unclassified_pattern_diagnostic_items(segment_id);
        """
    )


def path_group(relative_path: str | None) -> str:
    path = relative_path or "unknown"
    if "/" in path:
        return path.split("/", 1)[0]
    return path


def token_signature(text: str) -> str:
    features: list[str] = []
    for name, needle in TOKEN_FEATURES:
        if needle in text:
            features.append(name)
    if "$" in text:
        features.append("dollar_var")
    if "#!" in text or re.search(r"#\w+", text):
        features.append("formatting")
    if "[" in text and "]" in text:
        features.append("bracket_loc")
    return "+".join(features) if features else "other"


def risk_flags(text: str) -> list[str]:
    flags: list[str] = []
    if has_actual_mojibake(text):
        flags.append("mojibake")
    if spanish_hits(text) or SPANISH_LITERAL_RE.search(text):
        flags.append("spanish_literal")
    if "Custom('ES_" in text or 'Custom("ES_' in text:
        flags.append("gender_custom_loc")
    if "Select_CString" in text:
        flags.append("select_cstring")
    if len(text) > 260:
        flags.append("long_text")
    if "\\n" in text or "\n" in text:
        flags.append("multiline")
    return flags


def route_row(row: dict[str, Any], text: str, signature: str, flags: list[str]) -> tuple[str, str]:
    relative_path = str(row.get("relative_path") or "").lower()
    issue_kind = str(row.get("issue_kind") or "")
    bucket = str(row.get("queue_bucket") or "")
    group = path_group(relative_path)

    if "spanish_literal" in flags or "mojibake" in flags:
        return "dynamic_residual_literal_repair", "spanish_or_encoding_residue"
    if "gender_custom_loc" in flags:
        return "dynamic_gender_token_composer", "gender_custom_localization_in_dynamic_text"
    if "select_cstring" in flags:
        return "dynamic_select_cstring_composer", "select_cstring_requires_payload_or_context_policy"
    if group in {"effects_l_spanish.yml", "triggers"} or relative_path.endswith("effects_l_spanish.yml"):
        return "dynamic_rule_tooltip_expansion", "rules_tooltip_tokenized_surface"
    if any(name in signature for name in ("script_value", "get_trait", "get_activity_type", "get_scheme")):
        return "dynamic_tokenized_tooltip_expansion", "known_ck3_function_tokenized_surface"
    if "custom_loc" in signature and "long_text" in flags:
        return "dynamic_custom_loc_context_composer", "custom_localization_long_context"
    if "custom_localization" in bucket or "custom_localization" in issue_kind:
        return "dynamic_custom_loc_context_composer", "custom_localization_unclassified"
    if "bracket_loc" in signature and not flags:
        return "dynamic_plain_token_reference_candidate", "tokenized_reference_without_obvious_residue"
    return "dynamic_unclassified_review", "fallback_manual_pattern_review"


def fetch_rows(
    conn,
    *,
    shadow_run_ids: list[int],
    segment_state_run_id: int,
    partial_coverage_run_id: int | None,
) -> list[dict[str, Any]]:
    coverage_join = ""
    coverage_select = "NULL AS coverage_state, NULL AS total_issue_count, NULL AS open_issue_count, NULL AS blocked_issue_count,"
    params: list[Any] = list(shadow_run_ids)
    if partial_coverage_run_id is not None:
        coverage_select = (
            "coverage.coverage_state, coverage.total_issue_count, "
            "coverage.open_issue_count, coverage.blocked_issue_count,"
        )
        coverage_join = (
            "LEFT JOIN ml_issue_partial_coverage_items coverage "
            "ON coverage.segment_id = shadow.segment_id AND coverage.run_id = ?"
        )
        params.append(partial_coverage_run_id)
    params.append(segment_state_run_id)
    placeholders = ",".join("?" for _ in shadow_run_ids)
    rows = conn.execute(
        f"""
        SELECT
            shadow.id AS shadow_item_id,
            shadow.run_id AS shadow_run_id,
            shadow.queue_run_id,
            shadow.queue_item_id,
            shadow.ledger_run_id,
            shadow.ledger_item_id,
            shadow.segment_id,
            shadow.relative_path,
            shadow.source_key,
            shadow.source_line_number,
            shadow.queue_bucket,
            shadow.issue_kind,
            shadow.current_confirmed_text_hash,
            queue.evidence_text,
            queue.confirmed_text AS queue_confirmed_text,
            confirm.confirmed_text AS current_confirmed_text,
            output.portuguese_text AS output_text,
            state.state_group,
            state.final_state,
            {coverage_select}
            source.english_text,
            source.spanish_text
        FROM ml_issue_dynamic_ck3_pattern_shadow_items shadow
        JOIN ml_issue_review_queue_items queue ON queue.id = shadow.queue_item_id
        JOIN source_segments source ON source.id = shadow.segment_id
        LEFT JOIN output_segments output ON output.segment_id = shadow.segment_id
        LEFT JOIN segment_confirmations confirm
          ON confirm.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = shadow.segment_id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        JOIN segment_state_items state
          ON state.segment_id = shadow.segment_id
         AND state.run_id = ?
         AND state.state_group = 'pending'
        {coverage_join}
        WHERE shadow.run_id IN ({placeholders})
          AND shadow.shadow_allowed = 0
          AND shadow.block_reason = ?
        ORDER BY shadow.run_id, shadow.relative_path, shadow.source_line_number, shadow.source_key
        """,
        (*params[:-1], *params[-1:], TARGET_BLOCKER)
        if partial_coverage_run_id is None
        else (*shadow_run_ids, partial_coverage_run_id, segment_state_run_id, TARGET_BLOCKER),
    ).fetchall()
    return [dict(row) for row in rows]


def build_items(rows: list[dict[str, Any]], *, run_id: int) -> list[dict[str, Any]]:
    now = db.utc_now()
    items: list[dict[str, Any]] = []
    seen_segments: set[int] = set()
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id in seen_segments:
            continue
        seen_segments.add(segment_id)
        text = (
            row.get("current_confirmed_text")
            or row.get("output_text")
            or row.get("queue_confirmed_text")
            or row.get("evidence_text")
            or ""
        )
        signature = token_signature(text)
        flags = risk_flags(text)
        route_lane, route_reason = route_row(row, text, signature, flags)
        items.append(
            {
                "run_id": run_id,
                "shadow_run_id": int(row["shadow_run_id"]),
                "shadow_item_id": int(row["shadow_item_id"]),
                "queue_run_id": int(row["queue_run_id"]),
                "queue_item_id": int(row["queue_item_id"]),
                "ledger_run_id": int(row["ledger_run_id"]),
                "ledger_item_id": int(row["ledger_item_id"]),
                "segment_id": segment_id,
                "relative_path": row["relative_path"],
                "path_group": path_group(row["relative_path"]),
                "source_key": row["source_key"],
                "source_line_number": row.get("source_line_number"),
                "issue_kind": row.get("issue_kind") or "",
                "queue_bucket": row.get("queue_bucket"),
                "route_lane": route_lane,
                "route_reason": route_reason,
                "token_signature": signature,
                "risk_flags_json": json.dumps(flags, ensure_ascii=False),
                "coverage_state": row.get("coverage_state"),
                "total_issue_count": row.get("total_issue_count"),
                "open_issue_count": row.get("open_issue_count"),
                "blocked_issue_count": row.get("blocked_issue_count"),
                "text_sample": short(text),
                "created_at": now,
            }
        )
    return items


def insert_items(conn, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    fields = [
        "run_id",
        "shadow_run_id",
        "shadow_item_id",
        "queue_run_id",
        "queue_item_id",
        "ledger_run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "path_group",
        "source_key",
        "source_line_number",
        "issue_kind",
        "queue_bucket",
        "route_lane",
        "route_reason",
        "token_signature",
        "risk_flags_json",
        "coverage_state",
        "total_issue_count",
        "open_issue_count",
        "blocked_issue_count",
        "text_sample",
        "created_at",
    ]
    placeholders = ",".join("?" for _ in fields)
    conn.executemany(
        f"""
        INSERT INTO ml_issue_dynamic_unclassified_pattern_diagnostic_items (
            {", ".join(fields)}
        )
        VALUES ({placeholders})
        """,
        [tuple(item.get(field) for field in fields) for item in items],
    )


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    json_path: Path,
    run_id: int,
    shadow_run_ids: list[int],
    segment_state_run_id: int,
    partial_coverage_run_id: int | None,
    items: list[dict[str, Any]],
) -> None:
    route_counts = Counter(item["route_lane"] for item in items)
    signature_counts = Counter(item["token_signature"] for item in items)
    path_counts = Counter(item["path_group"] for item in items)
    coverage_counts = Counter(item.get("coverage_state") or "coverage_unknown" for item in items)

    fields = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "route_lane",
        "route_reason",
        "token_signature",
        "risk_flags_json",
        "coverage_state",
        "total_issue_count",
        "open_issue_count",
        "blocked_issue_count",
        "text_sample",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in items:
            writer.writerow({field: item.get(field) for field in fields})

    with json_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            {
                "rule_version": RULE_VERSION,
                "diagnostic_name": DIAGNOSTIC_NAME,
                "run_id": run_id,
                "shadow_run_ids": shadow_run_ids,
                "segment_state_run_id": segment_state_run_id,
                "partial_coverage_run_id": partial_coverage_run_id,
                "candidate_count": len(items),
                "route_counts": dict(route_counts.most_common()),
                "token_signature_counts": dict(signature_counts.most_common()),
                "path_group_counts": dict(path_counts.most_common()),
                "coverage_counts": dict(coverage_counts.most_common()),
                "items": items,
            },
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    examples_by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        lane = item["route_lane"]
        if len(examples_by_lane[lane]) < 8:
            examples_by_lane[lane].append(item)

    lines = [
        "Dynamic CK3 unclassified pattern diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Diagnostic run id: {run_id}",
        f"Shadow runs: {', '.join(str(value) for value in shadow_run_ids)}",
        f"Segment-state run id: {segment_state_run_id}",
        f"Partial coverage run id: {partial_coverage_run_id or 'none'}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(items):,}",
        "",
        "Route lanes:",
        *[f"- {key}: {value:,}" for key, value in route_counts.most_common()],
        "",
        "Coverage states:",
        *[f"- {key}: {value:,}" for key, value in coverage_counts.most_common()],
        "",
        "Top token signatures:",
        *[f"- {key}: {value:,}" for key, value in signature_counts.most_common(20)],
        "",
        "Top path groups:",
        *[f"- {key}: {value:,}" for key, value in path_counts.most_common(20)],
        "",
        "Interpretation:",
        "- This diagnostic does not close, repair, or write output.",
        "- High-volume clean tokenized lanes are candidates for a guarded false-reopen policy.",
        "- Residual/gender/select-cstring lanes should feed specialist composers before lifecycle closure.",
        "",
        "Examples by route lane:",
    ]
    for lane, examples in examples_by_lane.items():
        lines.append(f"{lane}:")
        for item in examples:
            lines.append(
                f"- segment={item['segment_id']} | {item['relative_path']}::{item['source_key']} | "
                f"{item['token_signature']} | {item['text_sample']}"
            )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    shadow_run_ids: list[int] | None = None,
    latest_shadow_limit: int = 3,
    segment_state_run_id: int | None = None,
    partial_coverage_run_id: int | None = None,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    txt_path, csv_path, json_path = report_paths(settings)
    with db.connect(settings) as conn:
        ensure_tables(conn)
        resolved_shadow_ids = shadow_run_ids or latest_shadow_run_ids(conn, latest_shadow_limit)
        if not resolved_shadow_ids:
            raise RuntimeError("No shadow runs found for dynamic diagnostic.")
        resolved_segment_state = segment_state_run_id or latest_segment_state_run_id(conn)
        resolved_coverage = partial_coverage_run_id
        if resolved_coverage is None:
            resolved_coverage = latest_partial_coverage_run_id(conn)

        run_id = conn.execute(
            """
            INSERT INTO ml_issue_dynamic_unclassified_pattern_diagnostic_runs (
                rule_version,
                diagnostic_name,
                shadow_run_ids_json,
                segment_state_run_id,
                partial_coverage_run_id,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                DIAGNOSTIC_NAME,
                json.dumps(resolved_shadow_ids, ensure_ascii=False),
                resolved_segment_state,
                resolved_coverage,
                started_at,
                started_at,
            ),
        ).lastrowid
        rows = fetch_rows(
            conn,
            shadow_run_ids=resolved_shadow_ids,
            segment_state_run_id=resolved_segment_state,
            partial_coverage_run_id=resolved_coverage,
        )
        items = build_items(rows, run_id=int(run_id))
        insert_items(conn, items)

        route_counts = Counter(item["route_lane"] for item in items)
        signature_counts = Counter(item["token_signature"] for item in items)
        path_counts = Counter(item["path_group"] for item in items)
        finished_at = db.utc_now()
        conn.execute(
            """
            UPDATE ml_issue_dynamic_unclassified_pattern_diagnostic_runs
            SET candidate_count = ?,
                route_lane_counts_json = ?,
                token_signature_counts_json = ?,
                path_group_counts_json = ?,
                report_path = ?,
                csv_path = ?,
                json_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(items),
                json.dumps(dict(route_counts.most_common()), ensure_ascii=False),
                json.dumps(dict(signature_counts.most_common()), ensure_ascii=False),
                json.dumps(dict(path_counts.most_common()), ensure_ascii=False),
                str(txt_path),
                str(csv_path),
                str(json_path),
                finished_at,
                finished_at,
                run_id,
            ),
        )
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            json_path=json_path,
            run_id=int(run_id),
            shadow_run_ids=resolved_shadow_ids,
            segment_state_run_id=resolved_segment_state,
            partial_coverage_run_id=resolved_coverage,
            items=items,
        )
        conn.commit()

    print("[issue_dynamic_unclassified_pattern_diagnostic] Diagnostic generated")
    print(f"[issue_dynamic_unclassified_pattern_diagnostic] Run id: {run_id}")
    print(f"[issue_dynamic_unclassified_pattern_diagnostic] Shadow runs: {resolved_shadow_ids}")
    print(f"[issue_dynamic_unclassified_pattern_diagnostic] Candidates: {len(items):,}")
    for key, value in Counter(item["route_lane"] for item in items).most_common():
        print(f"[issue_dynamic_unclassified_pattern_diagnostic] {key}: {value:,}")
    print(f"[issue_dynamic_unclassified_pattern_diagnostic] Report: {txt_path}")
    return {
        "run_id": int(run_id),
        "shadow_run_ids": resolved_shadow_ids,
        "candidate_count": len(items),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "json_path": str(json_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Route unclassified dynamic CK3 pattern blockers into specialist lanes.")
    parser.add_argument("--shadow-run-ids", default=None, help="Comma-separated shadow run ids. Defaults to latest runs.")
    parser.add_argument("--latest-shadow-limit", type=int, default=3)
    parser.add_argument("--segment-state-run-id", type=int, default=None)
    parser.add_argument("--partial-coverage-run-id", type=int, default=None)
    args = parser.parse_args()
    main(
        shadow_run_ids=parse_ids(args.shadow_run_ids),
        latest_shadow_limit=args.latest_shadow_limit,
        segment_state_run_id=args.segment_state_run_id,
        partial_coverage_run_id=args.partial_coverage_run_id,
    )
