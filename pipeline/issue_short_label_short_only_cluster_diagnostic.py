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


RULE_VERSION = "issue_short_label_short_only_cluster_diagnostic_v1"
DIAGNOSTIC_NAME = "short_label_short_only_open_router_v1"
TARGET_BUCKET = "short_only_open"
ISSUE_FAMILY = "short_label_style_microagent"

GENDER_MARKERS = (
    "Custom('ES_OA')",
    'Custom("ES_OA")',
    "Custom('ES_AO')",
    'Custom("ES_AO")',
    "Custom('ES_EA')",
    'Custom("ES_EA")',
)
DYNAMIC_MARKER_RE = re.compile(
    r"(\[[^\]]+\]|\$[^$]+\$|Select_CString|SelectLocalization|Custom\(|Concept\(|Get[A-Z]|ROOT\.|CHARACTER\.|TARGET\.|#\w+)",
    re.UNICODE,
)


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def latest_composition_run_id(conn, requested: int | None) -> int:
    if requested is not None:
        return requested
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_semantic_composition_diagnostic_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished short-label semantic composition diagnostic run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any], diagnostic_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_short_only_cluster_diagnostic_run_{diagnostic_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".json")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_short_only_cluster_diagnostic_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            diagnostic_name TEXT NOT NULL,
            composition_diagnostic_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            cluster_count INTEGER NOT NULL DEFAULT 0,
            route_lane_counts_json TEXT,
            cluster_counts_json TEXT,
            path_group_counts_json TEXT,
            surface_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            json_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_short_only_cluster_diagnostic_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            composition_diagnostic_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            path_group TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            issue_kind TEXT,
            route_lane TEXT NOT NULL,
            route_reason TEXT NOT NULL,
            cluster_key TEXT NOT NULL,
            domain TEXT NOT NULL,
            package TEXT NOT NULL,
            surface_bucket TEXT NOT NULL,
            has_ck3_token INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            text_length INTEGER NOT NULL DEFAULT 0,
            text_sample TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_short_only_cluster_diagnostic_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_only_cluster_diag_items_run
        ON ml_issue_short_label_short_only_cluster_diagnostic_items(run_id, route_lane, cluster_key);

        CREATE INDEX IF NOT EXISTS idx_short_only_cluster_diag_items_segment
        ON ml_issue_short_label_short_only_cluster_diagnostic_items(segment_id, route_lane);
        """
    )


def fetch_composition_run(conn, diagnostic_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_semantic_composition_diagnostic_runs
        WHERE id = ?
        """,
        (diagnostic_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Composition diagnostic run not found: {diagnostic_run_id}")
    return dict(row)


def fetch_rows(conn, *, diagnostic_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH run_meta AS (
            SELECT ledger_run_id
            FROM ml_issue_short_label_semantic_composition_diagnostic_runs
            WHERE id = ?
        ),
        ledger_pick AS (
            SELECT
                l2.segment_id,
                MIN(l2.id) AS ledger_item_id
            FROM ml_issue_ledger_items l2
            JOIN run_meta ON run_meta.ledger_run_id = l2.run_id
            WHERE l2.issue_family = ?
              AND l2.status = 'open'
            GROUP BY l2.segment_id
        )
        SELECT
            item.*,
            ledger.id AS ledger_item_id,
            ledger.issue_kind,
            ledger.token_impact,
            ledger.token_status,
            ledger.confidence_score,
            ledger.evidence_json AS ledger_evidence_json,
            ledger.evidence_text AS ledger_evidence_text
        FROM ml_issue_short_label_semantic_composition_diagnostic_items item
        LEFT JOIN ledger_pick pick ON pick.segment_id = item.segment_id
        LEFT JOIN ml_issue_ledger_items ledger
          ON ledger.id = pick.ledger_item_id
        WHERE item.run_id = ?
          AND item.composition_bucket = ?
        ORDER BY item.path_group, item.surface_bucket, item.relative_path, item.source_line_number, item.segment_id
        """,
        (diagnostic_run_id, ISSUE_FAMILY, diagnostic_run_id, TARGET_BUCKET),
    ).fetchall()
    return [dict(row) for row in rows]


def text_sample(row: dict[str, Any]) -> str:
    for key in ("confirmed_text", "output_text", "old_text", "ledger_evidence_text", "spanish_text", "english_text"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def short(text: str | None, limit: int = 180) -> str:
    value = (text or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    return value if len(value) <= limit else value[: limit - 3] + "..."


def route_row(row: dict[str, Any]) -> tuple[str, str]:
    evidence = parse_json_dict(row.get("ledger_evidence_json"))
    codes = {str(item) for item in evidence.get("issue_codes") or []}
    domain = str(evidence.get("domain") or "")
    path_group = str(row.get("path_group") or "")
    surface = str(row.get("surface_bucket") or "")
    issue_kind = str(row.get("issue_kind") or "")
    text = text_sample(row)
    has_token = int(row.get("has_ck3_token") or 0) == 1 or bool(DYNAMIC_MARKER_RE.search(text))

    if "mojibake_or_unexpected_script" in codes:
        return "encoding_or_script_repair", "mojibake_or_unexpected_script"
    if "spanish_residue" in codes or "spanish_residue_in_literal" in codes or "spanish" in issue_kind:
        return "residual_short_label_repair", issue_kind or "spanish_residue"
    if any(marker in text for marker in GENDER_MARKERS):
        return "gender_token_delegate", "gender_marker_in_short_label"
    if has_token:
        return "dynamic_ck3_expression_delegate", "ck3_token_or_dynamic_marker"
    if path_group == "titles" or domain == "domain_titles_names":
        return "title_policy_delegate", "title_or_name_domain"
    if domain == "domain_religion":
        return "religion_semantic_delegate", "religion_domain"
    if domain == "domain_culture":
        return "culture_semantic_delegate", "culture_domain"
    if surface in {"single_word", "short_phrase_2_3"}:
        return "compact_label_sample_candidate", "short_plain_surface"
    if surface == "compact_phrase_4_8":
        return "compact_ui_semantic_candidate", "compact_plain_surface"
    if surface == "long_9_plus":
        return "long_text_or_context_router", "long_short_only_surface"
    return "short_label_router_review", "fallback"


def cluster_key(row: dict[str, Any], route_lane: str, route_reason: str, domain: str, package: str) -> str:
    return "|".join(
        [
            route_lane,
            route_reason,
            str(row.get("path_group") or "unknown"),
            str(row.get("surface_bucket") or "unknown"),
            "token" if int(row.get("has_ck3_token") or 0) else "no_token",
            domain,
            package,
            str(row.get("issue_kind") or "unknown"),
        ]
    )


def build_items(rows: list[dict[str, Any]], *, run_id: int, diagnostic_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    created_at = db.utc_now()
    items: list[dict[str, Any]] = []
    for row in rows:
        evidence = parse_json_dict(row.get("ledger_evidence_json"))
        domain = str(evidence.get("domain") or "domain_unknown")
        package = str(evidence.get("package") or (str(row.get("relative_path") or "unknown").split("/", 1)[0]))
        route_lane, route_reason = route_row(row)
        sample = text_sample(row)
        items.append(
            {
                "run_id": run_id,
                "composition_diagnostic_run_id": diagnostic_run_id,
                "ledger_run_id": ledger_run_id,
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": int(row["segment_id"]),
                "relative_path": row["relative_path"],
                "path_group": row["path_group"],
                "source_key": row["source_key"],
                "source_line_number": row.get("source_line_number"),
                "issue_kind": row.get("issue_kind") or "",
                "route_lane": route_lane,
                "route_reason": route_reason,
                "cluster_key": cluster_key(row, route_lane, route_reason, domain, package),
                "domain": domain,
                "package": package,
                "surface_bucket": row["surface_bucket"],
                "has_ck3_token": int(row.get("has_ck3_token") or 0),
                "token_count": len(DYNAMIC_MARKER_RE.findall(sample)),
                "word_count": int(row.get("word_count") or 0),
                "text_length": int(row.get("text_length") or len(sample)),
                "text_sample": sample,
                "created_at": created_at,
            }
        )
    return items


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    json_path: Path,
    run_id: int,
    composition_run: dict[str, Any],
    items: list[dict[str, Any]],
    route_counts: Counter[str],
    cluster_counts: Counter[str],
    path_counts: Counter[str],
    surface_counts: Counter[str],
) -> None:
    fields = [
        "route_lane",
        "route_reason",
        "cluster_key",
        "segment_id",
        "relative_path",
        "path_group",
        "source_key",
        "source_line_number",
        "issue_kind",
        "domain",
        "package",
        "surface_bucket",
        "has_ck3_token",
        "token_count",
        "word_count",
        "text_length",
        "text_sample",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            writer.writerow(item)

    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        if len(examples[item["route_lane"]]) < 8:
            examples[item["route_lane"]].append(item)

    lines = [
        "Short-only Short-label Cluster Diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Composition diagnostic run id: {composition_run['id']}",
        f"Ledger run id: {composition_run['ledger_run_id']}",
        f"Segment-state run id: {composition_run['segment_state_run_id']}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(items):,}",
        f"- Clusters: {len(cluster_counts):,}",
        "",
        "Route lanes:",
    ]
    for lane, count in route_counts.most_common():
        lines.append(f"- {lane}: {count:,}")
    lines.extend(["", "Top clusters:"])
    for key, count in cluster_counts.most_common(30):
        lines.append(f"- {count:,} | {key}")
    lines.extend(["", "Top path groups:"])
    for key, count in path_counts.most_common(25):
        lines.append(f"- {key}: {count:,}")
    lines.extend(["", "Surface buckets:"])
    for key, count in surface_counts.most_common():
        lines.append(f"- {key}: {count:,}")
    lines.extend(
        [
            "",
            "Recommendation:",
            "- Do not build one generic short-label closer for this whole bucket.",
            "- Delegate title/religion/culture lanes to domain specialists.",
            "- Delegate dynamic/token lanes to dynamic CK3 or gender-token specialists.",
            "- The broad short-label learner should start with compact_label_sample_candidate and compact_ui_semantic_candidate only, after sample validation.",
            "- Long text/context rows should feed the composition coordinator, not a label-only policy.",
            "",
            "Examples by route lane:",
        ]
    )
    for lane, lane_examples in examples.items():
        lines.append(f"{lane}:")
        for item in lane_examples:
            lines.append(
                f"- segment={item['segment_id']} | {item['relative_path']}::{item['source_key']} | "
                f"{item['surface_bucket']} | {short(item['text_sample'], 140)}"
            )

    payload = {
        "rule_version": RULE_VERSION,
        "run_id": run_id,
        "composition_diagnostic_run_id": int(composition_run["id"]),
        "ledger_run_id": int(composition_run["ledger_run_id"]),
        "segment_state_run_id": int(composition_run["segment_state_run_id"]),
        "candidate_count": len(items),
        "cluster_count": len(cluster_counts),
        "route_lane_counts": dict(route_counts),
        "cluster_counts": dict(cluster_counts),
        "path_group_counts": dict(path_counts),
        "surface_counts": dict(surface_counts),
        "examples_by_route_lane": examples,
        "csv_path": str(csv_path),
        "report_path": str(txt_path),
    }
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(*, composition_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_run_id = latest_composition_run_id(conn, composition_run_id)
        composition_run = fetch_composition_run(conn, selected_run_id)
        rows = fetch_rows(conn, diagnostic_run_id=selected_run_id)

        cursor = conn.execute(
            """
            INSERT INTO ml_issue_short_label_short_only_cluster_diagnostic_runs (
                rule_version, diagnostic_name, composition_diagnostic_run_id,
                ledger_run_id, segment_state_run_id, started_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                DIAGNOSTIC_NAME,
                selected_run_id,
                int(composition_run["ledger_run_id"]),
                int(composition_run["segment_state_run_id"]),
                started_at,
                started_at,
            ),
        )
        run_id = int(cursor.lastrowid)
        items = build_items(
            rows,
            run_id=run_id,
            diagnostic_run_id=selected_run_id,
            ledger_run_id=int(composition_run["ledger_run_id"]),
        )

        route_counts = Counter(item["route_lane"] for item in items)
        cluster_counts = Counter(item["cluster_key"] for item in items)
        path_counts = Counter(item["path_group"] for item in items)
        surface_counts = Counter(item["surface_bucket"] for item in items)
        txt_path, csv_path, json_path = report_paths(settings, selected_run_id)

        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_short_only_cluster_diagnostic_items (
                run_id, composition_diagnostic_run_id, ledger_run_id, ledger_item_id,
                segment_id, relative_path, path_group, source_key, source_line_number,
                issue_kind, route_lane, route_reason, cluster_key, domain, package,
                surface_bucket, has_ck3_token, token_count, word_count, text_length,
                text_sample, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item["run_id"],
                    item["composition_diagnostic_run_id"],
                    item["ledger_run_id"],
                    item["ledger_item_id"],
                    item["segment_id"],
                    item["relative_path"],
                    item["path_group"],
                    item["source_key"],
                    item["source_line_number"],
                    item["issue_kind"],
                    item["route_lane"],
                    item["route_reason"],
                    item["cluster_key"],
                    item["domain"],
                    item["package"],
                    item["surface_bucket"],
                    item["has_ck3_token"],
                    item["token_count"],
                    item["word_count"],
                    item["text_length"],
                    item["text_sample"],
                    item["created_at"],
                )
                for item in items
            ],
        )
        finished_at = db.utc_now()
        conn.execute(
            """
            UPDATE ml_issue_short_label_short_only_cluster_diagnostic_runs
            SET candidate_count = ?,
                cluster_count = ?,
                route_lane_counts_json = ?,
                cluster_counts_json = ?,
                path_group_counts_json = ?,
                surface_counts_json = ?,
                report_path = ?,
                csv_path = ?,
                json_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(items),
                len(cluster_counts),
                json.dumps(dict(route_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(cluster_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(path_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(surface_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(json_path),
                finished_at,
                finished_at,
                run_id,
            ),
        )
        conn.commit()

    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        json_path=json_path,
        run_id=run_id,
        composition_run=composition_run,
        items=items,
        route_counts=route_counts,
        cluster_counts=cluster_counts,
        path_counts=path_counts,
        surface_counts=surface_counts,
    )

    print("[issue_short_label_short_only_cluster_diagnostic] Diagnostic generated")
    print(f"[issue_short_label_short_only_cluster_diagnostic] Run id: {run_id}")
    print(f"[issue_short_label_short_only_cluster_diagnostic] Composition run id: {selected_run_id}")
    print(f"[issue_short_label_short_only_cluster_diagnostic] Candidates: {len(items):,}")
    for lane, count in route_counts.most_common(12):
        print(f"[issue_short_label_short_only_cluster_diagnostic] {lane}: {count:,}")
    print(f"[issue_short_label_short_only_cluster_diagnostic] Report: {txt_path}")
    return {
        "run_id": run_id,
        "composition_diagnostic_run_id": selected_run_id,
        "candidate_count": len(items),
        "route_lane_counts": dict(route_counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "json_path": str(json_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cluster short-only open short-label pending segments by route lane.")
    parser.add_argument("--composition-run-id", type=int, default=None)
    args = parser.parse_args()
    main(composition_run_id=args.composition_run_id)
