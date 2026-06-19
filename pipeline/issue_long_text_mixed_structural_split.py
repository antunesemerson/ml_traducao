from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_long_text_mixed_structural_split_v1"
POLICY_NAME = "long_text_mixed_structural_split_shadow_v1"
POLICY_STATUS = "shadow"
SOURCE_POLICY_NAME = "long_text_structural_subpolicy_shadow_v1"

CONCEPT_PARAGRAPH = "long_text_concept_paragraph_residual_rewrite"
LEXICAL_GENDER = "long_text_lexical_gender_select_cstring_split"
MIXED_OBJECT_PRONOUN = "long_text_mixed_object_pronoun_surface_repair"
MIXED_QUOTE_TOKEN = "long_text_mixed_quote_token_surface_repair"
SELECT_CSTRING_STRUCTURAL = "long_text_select_cstring_structural_literal_repair"

MICRO_CONCEPT_SEMANTIC = "long_text_concept_paragraph_semantic_microagent"
MICRO_CONCEPT_TOKEN_REF = "long_text_concept_link_reference_guard"
MICRO_LEXICAL_GENDER_SELECT = "long_text_lexical_gender_select_cstring_microagent"
MICRO_SUBJECT_REFERENCE = "long_text_subject_reference_token_microagent"
MICRO_PREPOSITION_SURFACE = "long_text_preposition_surface_microagent"
MICRO_OBJECT_PRONOUN = "long_text_object_pronoun_case_microagent"
MICRO_LEXICAL_SURFACE = "long_text_lexical_surface_adjective_microagent"
MICRO_QUOTE_SURFACE = "long_text_quote_surface_microagent"
MICRO_SPEAKER_GENDER = "long_text_speaker_gender_alignment_microagent"
MICRO_SELECT_CSTRING_LITERAL = "long_text_select_cstring_literal_microagent"
MICRO_ARTICLE_TOKEN = "long_text_article_gender_token_microagent"
MICRO_SPANISH_RESIDUAL = "long_text_spanish_residual_semantic_microagent"
MICRO_UNCLASSIFIED = "long_text_unclassified_mixed_structural_microagent"

SPANISH_MARKERS = (
    " quiera",
    " gorrona",
    " gorr\u00f3n",
    " otra ",
    " otro ",
)


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def parse_json_obj(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return payload if isinstance(payload, dict) else {"value": payload}


def latest_shadow_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_long_text_structural_subpolicy_shadow_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND policy_status = 'shadow'
          AND blocked_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (SOURCE_POLICY_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No mixed structural shadow blockers found for {SOURCE_POLICY_NAME!r}.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_mixed_structural_split_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            structural_shadow_run_id INTEGER NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            split_unit_count INTEGER NOT NULL DEFAULT 0,
            split_ready_count INTEGER NOT NULL DEFAULT 0,
            needs_token_policy_count INTEGER NOT NULL DEFAULT 0,
            needs_semantic_review_count INTEGER NOT NULL DEFAULT 0,
            needs_more_evidence_count INTEGER NOT NULL DEFAULT 0,
            microagent_counts_json TEXT,
            status_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_mixed_structural_split_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            structural_shadow_run_id INTEGER NOT NULL,
            structural_shadow_item_id INTEGER NOT NULL,
            source_checkpoint_run_id INTEGER NOT NULL,
            source_checkpoint_item_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            original_subpolicy_name TEXT NOT NULL,
            original_block_reason TEXT,
            repair_route TEXT NOT NULL,
            microagent_key TEXT NOT NULL,
            micro_issue_kind TEXT NOT NULL,
            split_status TEXT NOT NULL,
            split_action TEXT NOT NULL,
            split_ready INTEGER NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 0,
            current_text_hash TEXT NOT NULL,
            corrected_text_hash TEXT NOT NULL,
            token_delta_json TEXT,
            reasons_json TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_long_text_mixed_structural_split_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], structural_shadow_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_long_text_mixed_structural_split_shadow_run_{structural_shadow_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_rows(conn, *, structural_shadow_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            shadow.id AS structural_shadow_item_id,
            shadow.run_id AS structural_shadow_run_id,
            shadow.checkpoint_run_id AS source_checkpoint_run_id,
            shadow.checkpoint_item_id AS source_checkpoint_item_id,
            shadow.decision_run_id,
            shadow.decision_id,
            shadow.queue_item_id,
            shadow.ledger_item_id,
            shadow.segment_id,
            shadow.relative_path,
            shadow.source_key,
            shadow.source_line_number,
            shadow.repair_route,
            shadow.subpolicy_name AS original_subpolicy_name,
            shadow.block_reason AS original_block_reason,
            shadow.token_delta_json,
            shadow.notes AS shadow_notes,
            checkpoint.current_text,
            checkpoint.corrected_text,
            checkpoint.notes AS decision_notes,
            checkpoint.reasons_json AS checkpoint_reasons_json
        FROM ml_issue_long_text_structural_subpolicy_shadow_items shadow
        JOIN ml_issue_long_text_repair_route_checkpoint_items checkpoint
          ON checkpoint.id = shadow.checkpoint_item_id
        WHERE shadow.run_id = ?
          AND shadow.shadow_ready = 0
        ORDER BY shadow.subpolicy_name, shadow.relative_path, shadow.source_line_number, shadow.source_key
        """,
        (structural_shadow_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def has_added(delta: dict[str, Any], marker: str) -> bool:
    return any(marker in str(item) for item in delta.get("added") or [])


def has_removed(delta: dict[str, Any], marker: str) -> bool:
    return any(marker in str(item) for item in delta.get("removed") or [])


def has_spanish_literal(text: str) -> bool:
    low = f" {text.casefold()} "
    return any(marker in low for marker in SPANISH_MARKERS)


def unit(
    *,
    microagent_key: str,
    micro_issue_kind: str,
    split_status: str,
    split_action: str,
    split_ready: int,
    priority: int,
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "microagent_key": microagent_key,
        "micro_issue_kind": micro_issue_kind,
        "split_status": split_status,
        "split_action": split_action,
        "split_ready": split_ready,
        "priority": priority,
        "reasons": reasons,
    }


def decompose(row: dict[str, Any]) -> list[dict[str, Any]]:
    original = row["original_subpolicy_name"]
    current = row.get("current_text") or ""
    corrected = row.get("corrected_text") or ""
    notes = row.get("decision_notes") or ""
    delta = parse_json_obj(row.get("token_delta_json"))
    units: list[dict[str, Any]] = []

    if original == CONCEPT_PARAGRAPH:
        units.append(
            unit(
                microagent_key=MICRO_CONCEPT_SEMANTIC,
                micro_issue_kind="semantic_paragraph_rewrite",
                split_status="needs_semantic_review",
                split_action="route_to_semantic_context_voter",
                split_ready=0,
                priority=70,
                reasons=["long_text_final_paragraph_semantic_delta", notes],
            )
        )
        if has_added(delta, "$game_concept_"):
            units.append(
                unit(
                    microagent_key=MICRO_CONCEPT_TOKEN_REF,
                    micro_issue_kind="concept_reference_token_delta",
                    split_status="needs_token_policy",
                    split_action="route_to_concept_reference_guard",
                    split_ready=0,
                    priority=65,
                    reasons=["concept_reference_added", json.dumps(delta, ensure_ascii=False, sort_keys=True)],
                )
            )

    elif original == LEXICAL_GENDER:
        units.append(
            unit(
                microagent_key=MICRO_LEXICAL_GENDER_SELECT,
                micro_issue_kind="lexical_gender_select_cstring_build",
                split_status="needs_token_policy",
                split_action="build_lexical_gender_select_cstring_pattern",
                split_ready=0,
                priority=80,
                reasons=["lexical_gender_requires_whole_word_split", notes],
            )
        )
        if has_added(delta, "GetSheHe") or has_removed(delta, "GetWomanMan"):
            units.append(
                unit(
                    microagent_key=MICRO_SUBJECT_REFERENCE,
                    micro_issue_kind="subject_reference_token_rewrite",
                    split_status="needs_token_policy",
                    split_action="build_subject_reference_token_pattern",
                    split_ready=0,
                    priority=75,
                    reasons=["subject_reference_token_delta", json.dumps(delta, ensure_ascii=False, sort_keys=True)],
                )
            )
        if "à/ao" in current and " a " in corrected:
            units.append(
                unit(
                    microagent_key=MICRO_PREPOSITION_SURFACE,
                    micro_issue_kind="preposition_surface_normalization",
                    split_status="split_ready",
                    split_action="observe_preposition_surface_repair_shadow",
                    split_ready=1,
                    priority=45,
                    reasons=["surface_only_inside_mixed_row", "a_ao_to_a"],
                )
            )

    elif original == MIXED_OBJECT_PRONOUN:
        if has_removed(delta, "GetSheHe") and has_added(delta, "GetHerHim"):
            units.append(
                unit(
                    microagent_key=MICRO_OBJECT_PRONOUN,
                    micro_issue_kind="object_pronoun_case_repair",
                    split_status="split_ready",
                    split_action="route_to_existing_object_pronoun_case_policy",
                    split_ready=1,
                    priority=85,
                    reasons=["exact_pronoun_case_component_inside_mixed_row"],
                )
            )
        units.append(
            unit(
                microagent_key=MICRO_LEXICAL_SURFACE,
                micro_issue_kind="lexical_surface_adjective_repair",
                split_status="needs_more_evidence",
                split_action="collect_lexical_surface_adjective_examples",
                split_ready=0,
                priority=55,
                reasons=["surface_or_semantic_adjective_delta", notes],
            )
        )

    elif original == MIXED_QUOTE_TOKEN:
        if "«" in current or "»" in current or '"…' in corrected or '".' in corrected:
            units.append(
                unit(
                    microagent_key=MICRO_QUOTE_SURFACE,
                    micro_issue_kind="quote_surface_normalization",
                    split_status="split_ready",
                    split_action="observe_quote_surface_normalization_shadow",
                    split_ready=1,
                    priority=50,
                    reasons=["quote_surface_component_inside_mixed_row"],
                )
            )
        if has_removed(delta, "GetSheHe") and has_added(delta, "GetHerHim"):
            units.append(
                unit(
                    microagent_key=MICRO_OBJECT_PRONOUN,
                    micro_issue_kind="object_pronoun_case_repair",
                    split_status="split_ready",
                    split_action="route_to_existing_object_pronoun_case_policy",
                    split_ready=1,
                    priority=85,
                    reasons=["object_pronoun_component_inside_quote_row"],
                )
            )
        if has_added(delta, "GetWomanMan") or has_added(delta, "Custom('ES_") or has_removed(delta, "Custom('ES_"):
            units.append(
                unit(
                    microagent_key=MICRO_SPEAKER_GENDER,
                    micro_issue_kind="speaker_gender_token_alignment",
                    split_status="needs_token_policy",
                    split_action="build_speaker_gender_alignment_policy",
                    split_ready=0,
                    priority=78,
                    reasons=["speaker_gender_scope_or_article_delta", json.dumps(delta, ensure_ascii=False, sort_keys=True)],
                )
            )

    elif original == SELECT_CSTRING_STRUCTURAL:
        if "Select_CString" in current or "Select_CString" in corrected:
            units.append(
                unit(
                    microagent_key=MICRO_SELECT_CSTRING_LITERAL,
                    micro_issue_kind="select_cstring_literal_translation",
                    split_status="needs_token_policy",
                    split_action="build_select_cstring_literal_translation_policy",
                    split_ready=0,
                    priority=82,
                    reasons=["select_cstring_literal_or_payload_component", notes],
                )
            )
        if has_removed(delta, "GetSheHe") and has_added(delta, "GetHerHim"):
            units.append(
                unit(
                    microagent_key=MICRO_OBJECT_PRONOUN,
                    micro_issue_kind="object_pronoun_case_repair",
                    split_status="split_ready",
                    split_action="route_to_existing_object_pronoun_case_policy",
                    split_ready=1,
                    priority=85,
                    reasons=["object_pronoun_component_inside_select_cstring_row"],
                )
            )
        if has_removed(delta, "Custom('ES_OA')") and has_added(delta, "Custom('ES_XA')"):
            units.append(
                unit(
                    microagent_key=MICRO_ARTICLE_TOKEN,
                    micro_issue_kind="article_gender_token_swap",
                    split_status="needs_token_policy",
                    split_action="build_article_gender_token_swap_policy",
                    split_ready=0,
                    priority=76,
                    reasons=["es_oa_to_es_xa_article_component", json.dumps(delta, ensure_ascii=False, sort_keys=True)],
                )
            )
        if has_spanish_literal(current) or has_spanish_literal(corrected):
            units.append(
                unit(
                    microagent_key=MICRO_SPANISH_RESIDUAL,
                    micro_issue_kind="visible_spanish_literal_semantic_repair",
                    split_status="needs_semantic_review",
                    split_action="route_to_spanish_residual_semantic_policy",
                    split_ready=0,
                    priority=72,
                    reasons=["visible_spanish_literal_component", notes],
                )
            )

    if not units:
        units.append(
            unit(
                microagent_key=MICRO_UNCLASSIFIED,
                micro_issue_kind="unclassified_mixed_structural",
                split_status="needs_more_evidence",
                split_action="manual_cluster_before_microagent",
                split_ready=0,
                priority=10,
                reasons=["no_decomposition_rule_matched", original, notes],
            )
        )
    return units


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    source_rows: list[dict[str, Any]],
    units: list[dict[str, Any]],
    started_at: datetime,
) -> None:
    fields = [
        "split_item_id",
        "structural_shadow_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "original_subpolicy_name",
        "original_block_reason",
        "repair_route",
        "microagent_key",
        "micro_issue_kind",
        "split_status",
        "split_action",
        "split_ready",
        "priority",
        "token_delta_json",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in units:
            writer.writerow({field: row.get(field) for field in fields})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in units:
            payload = {
                **{field: row.get(field) for field in fields},
                "reasons": row.get("reasons") or [],
                "current_preview": short(row.get("current_text"), 220),
                "corrected_preview": short(row.get("corrected_text"), 220),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    by_status = Counter(row["split_status"] for row in units)
    by_microagent = Counter(row["microagent_key"] for row in units)
    by_source = Counter(row["original_subpolicy_name"] for row in source_rows)
    ready_units = [row for row in units if row["split_ready"]]
    blocked_units = [row for row in units if not row["split_ready"]]
    lines = [
        "Issue long-text mixed structural split",
        f"Rule version: {RULE_VERSION}",
        f"Policy: {POLICY_NAME} ({POLICY_STATUS})",
        f"Split run id: {run_id}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Mixed blockers: {len(source_rows):,}",
        f"- Split units: {len(units):,}",
        f"- Split-ready units: {len(ready_units):,}",
        f"- Needs token policy: {by_status['needs_token_policy']:,}",
        f"- Needs semantic review: {by_status['needs_semantic_review']:,}",
        f"- Needs more evidence: {by_status['needs_more_evidence']:,}",
        f"- By original subpolicy: {json.dumps(dict(by_source), ensure_ascii=False, sort_keys=True)}",
        f"- By split status: {json.dumps(dict(by_status), ensure_ascii=False, sort_keys=True)}",
        f"- By microagent: {json.dumps(dict(by_microagent), ensure_ascii=False, sort_keys=True)}",
        "",
        "Split-ready units:",
    ]
    for row in ready_units[:30]:
        lines.extend(
            [
                (
                    f"- {row['microagent_key']} / {row['micro_issue_kind']} | "
                    f"{row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}"
                ),
                f"  action={row['split_action']}",
            ]
        )
    if not ready_units:
        lines.append("- none")
    lines.extend(["", "Units requiring another layer:"])
    for row in blocked_units[:40]:
        lines.extend(
            [
                (
                    f"- {row['microagent_key']} / {row['micro_issue_kind']} / {row['split_status']} | "
                    f"{row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}"
                ),
                f"  action={row['split_action']}",
            ]
        )
    if not blocked_units:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Split-only: no source/output read, no confirmation promotion, no segment-state closure.",
            "- One mixed segment can generate multiple partial units; this is expected.",
            "- Split-ready means ready for a future shadow/checkpoint, not ready for production.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, structural_shadow_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_run_id = structural_shadow_run_id or latest_shadow_run_id(conn)
        source_rows = fetch_rows(conn, structural_shadow_run_id=selected_run_id)
        if not source_rows:
            raise RuntimeError(f"Structural shadow run {selected_run_id} has no blocked mixed items.")

        split_units: list[dict[str, Any]] = []
        for row in source_rows:
            row_units = decompose(row)
            for item in row_units:
                item.update(
                    {
                        **row,
                        "split_ready": int(item["split_ready"]),
                        "reasons_json": json.dumps(item["reasons"], ensure_ascii=False, sort_keys=True),
                    }
                )
                split_units.append(item)

        by_microagent = Counter(row["microagent_key"] for row in split_units)
        by_status = Counter(row["split_status"] for row in split_units)
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_long_text_mixed_structural_split_runs (
                rule_version,
                structural_shadow_run_id,
                policy_name,
                policy_status,
                candidate_count,
                split_unit_count,
                split_ready_count,
                needs_token_policy_count,
                needs_semantic_review_count,
                needs_more_evidence_count,
                microagent_counts_json,
                status_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_run_id,
                POLICY_NAME,
                POLICY_STATUS,
                len(source_rows),
                len(split_units),
                by_status["split_ready"],
                by_status["needs_token_policy"],
                by_status["needs_semantic_review"],
                by_status["needs_more_evidence"],
                json.dumps(dict(by_microagent), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(by_status), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        run_id = int(cur.lastrowid)
        created_at = db.utc_now()
        for row in split_units:
            item_cur = conn.execute(
                """
                INSERT INTO ml_issue_long_text_mixed_structural_split_items (
                    run_id,
                    structural_shadow_run_id,
                    structural_shadow_item_id,
                    source_checkpoint_run_id,
                    source_checkpoint_item_id,
                    decision_run_id,
                    decision_id,
                    queue_item_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    original_subpolicy_name,
                    original_block_reason,
                    repair_route,
                    microagent_key,
                    micro_issue_kind,
                    split_status,
                    split_action,
                    split_ready,
                    priority,
                    current_text_hash,
                    corrected_text_hash,
                    token_delta_json,
                    reasons_json,
                    notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    int(row["structural_shadow_run_id"]),
                    int(row["structural_shadow_item_id"]),
                    int(row["source_checkpoint_run_id"]),
                    int(row["source_checkpoint_item_id"]),
                    int(row["decision_run_id"]),
                    int(row["decision_id"]),
                    int(row["queue_item_id"]),
                    int(row["ledger_item_id"]),
                    int(row["segment_id"]),
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["original_subpolicy_name"],
                    row.get("original_block_reason") or "",
                    row["repair_route"],
                    row["microagent_key"],
                    row["micro_issue_kind"],
                    row["split_status"],
                    row["split_action"],
                    int(row["split_ready"]),
                    int(row["priority"]),
                    sha256_text(row.get("current_text")),
                    sha256_text(row.get("corrected_text")),
                    row.get("token_delta_json") or "{}",
                    row["reasons_json"],
                    row.get("decision_notes") or row.get("shadow_notes") or "",
                    created_at,
                ),
            )
            row["split_item_id"] = int(item_cur.lastrowid)

        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            source_rows=source_rows,
            units=split_units,
            started_at=started_at,
        )
        conn.commit()

    print("[issue_long_text_mixed_structural_split] Split generated")
    print(f"[issue_long_text_mixed_structural_split] Rule version: {RULE_VERSION}")
    print(f"[issue_long_text_mixed_structural_split] Split run id: {run_id}")
    print(f"[issue_long_text_mixed_structural_split] Structural shadow run id: {selected_run_id}")
    print(f"[issue_long_text_mixed_structural_split] Mixed blockers: {len(source_rows):,}")
    print(f"[issue_long_text_mixed_structural_split] Split units: {len(split_units):,}")
    print(f"[issue_long_text_mixed_structural_split] Split ready: {by_status['split_ready']:,}")
    print(f"[issue_long_text_mixed_structural_split] Report: {txt_path}")
    return {
        "run_id": run_id,
        "structural_shadow_run_id": selected_run_id,
        "candidate_count": len(source_rows),
        "split_unit_count": len(split_units),
        "split_ready_count": by_status["split_ready"],
        "needs_token_policy_count": by_status["needs_token_policy"],
        "needs_semantic_review_count": by_status["needs_semantic_review"],
        "needs_more_evidence_count": by_status["needs_more_evidence"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Split mixed structural long-text blockers into partial microagent units.")
    parser.add_argument("--structural-shadow-run-id", type=int, default=None)
    args = parser.parse_args()
    main(structural_shadow_run_id=args.structural_shadow_run_id)
