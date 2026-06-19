from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
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


RULE_VERSION = "auto_confirmation_reopen_text_diagnostic_v2"
DEFAULT_LABEL_FAMILY = "mechanical_text_replacement"

SPANISH_LITERAL_HINTS = {
    "sabes",
    "sabe",
    "inspiras",
    "inspira",
    "posees",
    "posee",
    "hacerte",
    "hacerse",
    "verdadera",
    "verdadero",
    "autentica",
    "autentico",
    "heredera",
    "heredero",
    "cielos",
}

WEAK_AUTO_SPANISH_LITERAL_HINTS = SPANISH_LITERAL_HINTS | {
    "al",
    "del",
    "de la",
    "el",
    "la",
    "las",
    "los",
    "si",
    "solo",
    "un",
    "una",
    "construir",
    "coste",
    "edificios",
    "lineas",
    "líneas",
    "presupuesto",
    "promulgar",
    "puede",
    "puedes",
    "aportaste",
    "aportó",
    "aportÃ³",
    "decidiste",
    "decidió",
    "decidiÃ³",
    "deseaste",
    "deseó",
    "deseÃ³",
    "ganarás",
    "ganarÃ¡s",
    "ganará",
    "ganarÃ¡",
    "ganas",
    "gana",
    "intentará",
    "intentarÃ¡",
    "intentarás",
    "intentarÃ¡s",
    "ridiculizaste",
    "ridiculizó",
    "ridiculizÃ³",
    "te luciste",
    "se lució",
    "se luciÃ³",
    "viaje",
    "viajar",
    "forajida",
    "forajido",
    "funcionaria",
    "funcionario",
    "heredera",
    "heredero",
    "cazadora",
    "cazador",
    "renegada",
    "renegado",
    "cochina",
    "cerdo",
    "cautiverio",
}


def latest_queue_run_id(conn, *, label_family: str) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_guarded_queue_runs
        WHERE finished_at IS NOT NULL
          AND label_family = ?
          AND selected_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (label_family,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No {label_family} guarded queue found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_rows(conn, *, queue_run_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    queue_run = conn.execute(
        "SELECT * FROM auto_confirmation_reopen_guarded_queue_runs WHERE id = ?",
        (queue_run_id,),
    ).fetchone()
    if queue_run is None:
        raise RuntimeError(f"Queue run not found: {queue_run_id}")
    rows = conn.execute(
        """
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            output.portuguese_text,
            confirmation.confirmed_text
        FROM auto_confirmation_reopen_guarded_queue_items item
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
        ORDER BY item.queue_rank, item.relative_path, item.source_line_number
        """,
        (queue_run_id,),
    ).fetchall()
    return dict(queue_run), [dict(row) for row in rows]


def select_cstring_count(text: str) -> int:
    return text.count("Select_CString(") + text.count("LocalPlayerString(")


def concept_link_count(text: str) -> int:
    return text.count("Concept(") + len(re.findall(r"\[[^\]]+\|[lU]?[EG]\]", text))


def spanish_literal_hint_count(text: str, *, hints: set[str] | None = None) -> int:
    lowered = text.lower()
    selected_hints = hints or SPANISH_LITERAL_HINTS
    return sum(1 for hint in selected_hints if re.search(rf"(?<!\w){re.escape(hint)}(?!\w)", lowered))


def path_group(path: str) -> str:
    if path == "nicknames_l_spanish.yml":
        return "nicknames"
    if path.startswith("event_localization/hold_court_events/"):
        return "hold_court"
    if path.startswith("diarchies/"):
        return "diarchies"
    if path == "effects_l_spanish.yml":
        return "effects"
    if path.startswith("triggers/"):
        return "triggers"
    if path.startswith("interactions"):
        return "interactions"
    if "/" in path:
        return path.split("/", 1)[0]
    return path


def classify_weak_auto_row(row: dict[str, Any]) -> tuple[str, str, str, str, list[str]]:
    text = row.get("confirmed_text") or row.get("portuguese_text") or ""
    select_count = select_cstring_count(text)
    concept_count = concept_link_count(text)
    literal_hints = spanish_literal_hint_count(text, hints=WEAK_AUTO_SPANISH_LITERAL_HINTS)
    word_count = int(row.get("word_count") or 0)
    issue_count = int(row.get("issue_count") or 0)
    high_issue_count = int(row.get("high_issue_count") or 0)
    outside_letters = has_visible_letters_outside_tokens(text)
    source_outside_letters = (
        has_visible_letters_outside_tokens(row.get("english_text") or "")
        or has_visible_letters_outside_tokens(row.get("spanish_text") or "")
    )
    embedded_literals = has_embedded_visible_token_literals(text)
    spanish_custom_helper = has_spanish_custom_localization_helper(text) or is_spanish_custom_localization_definition(row)
    reasons: list[str] = []
    if select_count:
        reasons.append(f"select_cstring_count:{select_count}")
    if concept_count:
        reasons.append(f"concept_link_count:{concept_count}")
    if literal_hints:
        reasons.append(f"weak_auto_spanish_literal_hint_count:{literal_hints}")
    if not outside_letters and source_outside_letters:
        reasons.append("source_has_visible_text_but_candidate_surface_is_token_only")
    if spanish_custom_helper:
        reasons.append("spanish_custom_localization_helper")
    if issue_count:
        reasons.append("issue_signal_present")
    if high_issue_count:
        reasons.append("high_issue_signal_present")
    if row.get("output_match_kind") == "display_equivalent_escape_delta":
        reasons.append("display_equivalent_escape_delta")

    if issue_count or high_issue_count:
        if literal_hints and select_count:
            return (
                "weak_auto_dynamic_spanish_literal",
                "weak_auto_dynamic_spanish_literal_boundary",
                "manual_boundary_review",
                "high",
                reasons,
            )
        if literal_hints:
            return (
                "weak_auto_residual_spanish_literal",
                "weak_auto_residual_spanish_boundary",
                "manual_boundary_review",
                "high",
                reasons,
            )
        if select_count >= 2:
            return (
                "weak_auto_dynamic_issue_signal",
                "weak_auto_dynamic_issue_boundary",
                "manual_boundary_review",
                "high",
                reasons,
            )
        return (
            "weak_auto_issue_signal",
            "weak_auto_issue_boundary",
            "manual_boundary_review",
            "high",
                reasons,
            )

    if not outside_letters and source_outside_letters:
        return (
            "weak_auto_token_surface_semantic_delta",
            "weak_auto_token_surface_semantic_boundary",
            "needs_subagent_before_policy",
            "high",
            reasons,
        )

    if not outside_letters:
        if spanish_custom_helper:
            return (
                "weak_auto_custom_loc_helper_token",
                "weak_auto_custom_loc_helper_boundary",
                "needs_subagent_before_policy",
                "medium",
                reasons,
            )
        if embedded_literals:
            if literal_hints:
                return (
                    "weak_auto_embedded_spanish_literal_token",
                    "weak_auto_embedded_literal_token_specialist",
                    "needs_subagent_before_policy",
                    "high",
                    reasons,
                )
            return (
                "weak_auto_embedded_literal_token_exact",
                "weak_auto_embedded_literal_token_specialist",
                "needs_subagent_before_policy",
                "medium",
                reasons,
            )
        return (
            "weak_auto_empty_or_token_exact",
            "weak_auto_empty_or_token_sampler",
            "candidate_short_ui_policy_sampling",
            "low",
            reasons,
        )

    if literal_hints:
        if select_count:
            return (
                "weak_auto_dynamic_spanish_literal",
                "weak_auto_dynamic_spanish_literal_boundary",
                "needs_subagent_before_policy",
                "high",
                reasons,
            )
        return (
            "weak_auto_residual_spanish_literal",
            "weak_auto_residual_spanish_boundary",
            "needs_subagent_before_policy",
            "medium",
            reasons,
        )

    if row.get("output_match_kind") == "display_equivalent_escape_delta":
        return (
            "weak_auto_escape_delta_equivalence",
            "weak_auto_escape_delta_sampler",
            "candidate_short_ui_policy_sampling",
            "low",
            reasons,
        )

    if word_count <= 0:
        return (
            "weak_auto_empty_or_token_exact",
            "weak_auto_empty_or_token_sampler",
            "candidate_short_ui_policy_sampling",
            "low",
            reasons,
        )

    if word_count <= 3:
        return (
            "weak_auto_tiny_exact",
            "weak_auto_short_exact_sampler",
            "candidate_short_ui_policy_sampling",
            "low",
            reasons,
        )

    if concept_count and word_count <= 8:
        return (
            "weak_auto_short_concept_exact",
            "weak_auto_short_concept_sampler",
            "candidate_short_ui_policy_sampling",
            "medium",
            reasons,
        )

    if select_count >= 2:
        return (
            "weak_auto_dynamic_exact",
            "weak_auto_dynamic_context_sampler",
            "manual_sampling_before_policy",
            "medium",
            reasons,
        )

    return (
        "weak_auto_general_exact",
        "weak_auto_confirmation_sampler",
        "manual_sampling_before_policy",
        "medium",
        reasons,
    )


def classify_row(row: dict[str, Any], *, label_family: str = DEFAULT_LABEL_FAMILY) -> tuple[str, str, str, str, list[str]]:
    if label_family == "weak_auto_confirmation":
        return classify_weak_auto_row(row)

    path = row["relative_path"]
    key = row["source_key"]
    label = row.get("confirmation_label") or ""
    text = row.get("confirmed_text") or row.get("portuguese_text") or ""
    select_count = select_cstring_count(text)
    concept_count = concept_link_count(text)
    literal_hints = spanish_literal_hint_count(text)
    word_count = int(row.get("word_count") or 0)
    reasons: list[str] = []
    if select_count:
        reasons.append(f"select_cstring_count:{select_count}")
    if concept_count:
        reasons.append(f"concept_link_count:{concept_count}")
    if literal_hints:
        reasons.append(f"spanish_literal_hint_count:{literal_hints}")
    if int(row.get("issue_count") or 0) > 0:
        reasons.append("issue_signal_present")

    if int(row.get("issue_count") or 0) > 0 or int(row.get("high_issue_count") or 0) > 0:
        return (
            "manual_issue_boundary",
            "auto_confirmation_text_issue_boundary",
            "manual_boundary_review",
            "high",
            reasons,
        )
    if path == "nicknames_l_spanish.yml" and select_count:
        return (
            "nickname_dynamic_literal_replacement",
            "nickname_select_cstring_literal_specialist",
            "needs_subagent_before_policy",
            "high" if literal_hints else "medium",
            reasons,
        )
    if path.startswith("event_localization/hold_court_events/"):
        return (
            "hold_court_dialogue_literal_replacement",
            "hold_court_dialogue_literal_specialist",
            "needs_subagent_before_policy",
            "medium",
            reasons,
        )
    if path.startswith("diarchies/") and word_count <= 8:
        return (
            "short_ui_relation_score_literal",
            "short_ui_relation_score_literal_specialist",
            "candidate_short_ui_policy_sampling",
            "medium" if select_count else "low",
            reasons,
        )
    if "visible_" in label or concept_count:
        return (
            "visible_concept_or_glossary_literal",
            "visible_concept_literal_boundary",
            "needs_subagent_before_policy",
            "medium",
            reasons,
        )
    if path.startswith("interactions") or path.startswith("triggers/") or path == "effects_l_spanish.yml":
        return (
            "short_tooltip_logic_literal",
            "short_tooltip_logic_literal_specialist",
            "candidate_short_ui_policy_sampling",
            "medium",
            reasons,
        )
    if path.startswith("event_localization/") or path.startswith("dlc/") or path.endswith("_events_l_spanish.yml"):
        return (
            "event_narrative_literal_replacement",
            "event_narrative_literal_boundary",
            "manual_sampling_before_policy",
            "medium",
            reasons,
        )
    return (
        "other_text_literal_replacement",
        "auto_confirmation_text_replacement_reconciler",
        "manual_sampling_before_policy",
        "medium",
        reasons,
    )


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    diagnostic_run_id: int,
    queue_run: dict[str, Any],
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> None:
    fieldnames = [
        "diagnostic_item_id",
        "queue_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "confirmation_label",
        "text_subfamily",
        "suggested_agent_key",
        "recommendation",
        "risk_level",
        "select_cstring_count",
        "concept_link_count",
        "spanish_literal_hint_count",
        "ui_short_text",
        "issue_count",
        "model_safe_probability",
        "review_priority",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "output_preview": short(row.get("portuguese_text")),
                "confirmed_preview": short(row.get("confirmed_text")),
                "reasons": row.get("split_reasons"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    by_subfamily = Counter(row["text_subfamily"] for row in rows)
    by_recommendation = Counter(row["recommendation"] for row in rows)
    by_agent = Counter(row["suggested_agent_key"] for row in rows)
    by_risk = Counter(row["risk_level"] for row in rows)
    lines = [
        "Auto-confirmation text replacement diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Diagnostic run id: {diagnostic_run_id}",
        f"Queue run id: {queue_run['id']}",
        f"Audit run id: {queue_run['audit_run_id']}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Rows inspected: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in by_recommendation.most_common()],
        "",
        "By subfamily:",
        *[f"- {key}: {value:,}" for key, value in by_subfamily.most_common()],
        "",
        "By suggested agent:",
        *[f"- {key}: {value:,}" for key, value in by_agent.most_common()],
        "",
        "By risk:",
        *[f"- {key}: {value:,}" for key, value in by_risk.most_common()],
        "",
        "Priority samples:",
    ]
    for row in sorted(rows, key=lambda item: (item["risk_level"] != "high", -float(item["review_priority"]), item["relative_path"]))[:40]:
        lines.extend(
            [
                (
                    f"- {row['text_subfamily']} | {row['recommendation']} | "
                    f"{row['relative_path']}:{row['source_line_number']}:{row['source_key']}"
                ),
                (
                    f"  agent={row['suggested_agent_key']}; risk={row['risk_level']}; "
                    f"label={row.get('confirmation_label')}; hints={row['spanish_literal_hint_count']}"
                ),
                f"  OUT: {short(row.get('portuguese_text'))}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This diagnostic only classifies text replacement evidence into subfamilies.",
            "- It does not close lifecycle states, promote policy, alter confirmations, or write output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None, label_family: str = DEFAULT_LABEL_FAMILY) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn, label_family=label_family)
        queue_run, rows = fetch_rows(conn, queue_run_id=selected_queue_run_id)
        if queue_run.get("label_family") != label_family:
            raise RuntimeError(f"Queue run {selected_queue_run_id} is not {label_family!r}.")
        for row in rows:
            text = row.get("confirmed_text") or row.get("portuguese_text") or ""
            subfamily, agent, recommendation, risk, reasons = classify_row(row, label_family=label_family)
            hint_count = spanish_literal_hint_count(
                text,
                hints=WEAK_AUTO_SPANISH_LITERAL_HINTS
                if label_family == "weak_auto_confirmation"
                else SPANISH_LITERAL_HINTS,
            )
            row["text_subfamily"] = subfamily
            row["suggested_agent_key"] = agent
            row["recommendation"] = recommendation
            row["risk_level"] = risk
            row["select_cstring_count"] = select_cstring_count(text)
            row["concept_link_count"] = concept_link_count(text)
            row["spanish_literal_hint_count"] = hint_count
            row["ui_short_text"] = 1 if int(row.get("word_count") or 0) <= 8 else 0
            row["split_reasons"] = reasons

        subfamilies = {row["text_subfamily"] for row in rows}
        recommendations = Counter(row["recommendation"] for row in rows)
        now = datetime.now().isoformat(timespec="seconds")
        txt_path, csv_path, jsonl_path = report_paths(settings)
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_diagnostic_runs (
                rule_version,
                queue_run_id,
                audit_run_id,
                label_family,
                total_items,
                subfamily_count,
                needs_subagent_count,
                candidate_short_policy_count,
                manual_boundary_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_queue_run_id,
                queue_run.get("audit_run_id"),
                label_family,
                len(rows),
                len(subfamilies),
                recommendations["needs_subagent_before_policy"],
                recommendations["candidate_short_ui_policy_sampling"],
                recommendations["manual_boundary_review"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        diagnostic_run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO auto_confirmation_reopen_text_diagnostic_items (
                    run_id,
                    queue_run_id,
                    queue_item_id,
                    audit_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    confirmation_label,
                    text_subfamily,
                    suggested_agent_key,
                    recommendation,
                    risk_level,
                    select_cstring_count,
                    concept_link_count,
                    spanish_literal_hint_count,
                    ui_short_text,
                    issue_count,
                    model_safe_probability,
                    review_priority,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    diagnostic_run_id,
                    selected_queue_run_id,
                    row["id"],
                    row.get("audit_item_id"),
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row.get("confirmation_label"),
                    row["text_subfamily"],
                    row["suggested_agent_key"],
                    row["recommendation"],
                    row["risk_level"],
                    int(row["select_cstring_count"]),
                    int(row["concept_link_count"]),
                    int(row["spanish_literal_hint_count"]),
                    int(row["ui_short_text"]),
                    int(row.get("issue_count") or 0),
                    row.get("model_safe_probability"),
                    float(row.get("review_priority") or 0),
                    json.dumps(row["split_reasons"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            row["diagnostic_item_id"] = int(item_cursor.lastrowid)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            diagnostic_run_id=diagnostic_run_id,
            queue_run=queue_run,
            rows=rows,
            started_at=started_at,
        )
        conn.commit()

    print("[auto_confirmation_reopen_text_diagnostic] Diagnostic generated")
    print(f"[auto_confirmation_reopen_text_diagnostic] Run id: {diagnostic_run_id}")
    print(f"[auto_confirmation_reopen_text_diagnostic] Queue run id: {selected_queue_run_id}")
    print(f"[auto_confirmation_reopen_text_diagnostic] Rows: {len(rows):,}")
    for key, value in recommendations.most_common():
        print(f"[auto_confirmation_reopen_text_diagnostic] {key}: {value:,}")
    print(f"[auto_confirmation_reopen_text_diagnostic] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_diagnostic] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_diagnostic] JSONL: {jsonl_path}")
    return {
        "run_id": diagnostic_run_id,
        "queue_run_id": selected_queue_run_id,
        "rows": len(rows),
        "recommendations": dict(recommendations),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Split mechanical text replacement rows into risk subfamilies.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    parser.add_argument("--family", default=DEFAULT_LABEL_FAMILY)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id, label_family=args.family)
