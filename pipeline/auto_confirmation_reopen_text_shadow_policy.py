from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "auto_confirmation_reopen_text_shadow_policy_v1"
DEFAULT_POLICY_KEY = "hold_court_aptitude_tooltip"

POLICIES: dict[str, dict[str, str]] = {
    "hold_court_aptitude_tooltip": {
        "policy_name": "hold_court_aptitude_tooltip_shadow_v1",
        "agent_key": "hold_court_aptitude_tooltip_safe_candidate",
        "parent_agent_key": "hold_court_dialogue_literal_specialist",
        "text_subfamily": "hold_court_dialogue_literal_replacement",
        "shadow_action": "would_close_reopened_auto_confirmation_for_hold_court_aptitude",
    },
    "short_ui_relation_score": {
        "policy_name": "short_ui_relation_score_shadow_v1",
        "agent_key": "short_ui_relation_score_literal_specialist",
        "parent_agent_key": "auto_confirmation_reopen_reconciler",
        "text_subfamily": "short_ui_relation_score_literal",
        "shadow_action": "would_close_reopened_auto_confirmation_for_short_ui_relation_score",
    },
    "short_tooltip_artifact_honor_reason": {
        "policy_name": "short_tooltip_artifact_honor_reason_shadow_v1",
        "agent_key": "short_tooltip_logic_literal_specialist",
        "parent_agent_key": "auto_confirmation_reopen_reconciler",
        "text_subfamily": "short_tooltip_logic_literal",
        "shadow_action": "would_close_reopened_auto_confirmation_for_artifact_honor_reason",
    },
    "weak_auto_static_token_only": {
        "policy_name": "weak_auto_static_token_only_shadow_v1",
        "agent_key": "weak_auto_static_token_only_safe_candidate",
        "parent_agent_key": "weak_auto_empty_or_token_sampler",
        "text_subfamily": "weak_auto_empty_or_token_exact",
        "shadow_action": "would_close_reopened_auto_confirmation_for_static_token_only",
    },
    "weak_auto_embedded_possessive_runtime": {
        "policy_name": "weak_auto_embedded_possessive_runtime_shadow_v1",
        "agent_key": "weak_auto_embedded_possessive_runtime_safe_candidate",
        "parent_agent_key": "weak_auto_embedded_literal_token_specialist",
        "text_subfamily": "weak_auto_embedded_literal_token_exact",
        "shadow_action": "would_close_reopened_auto_confirmation_for_embedded_possessive_runtime",
    },
    "weak_auto_compact_ui_token_stack": {
        "policy_name": "weak_auto_compact_ui_token_stack_shadow_v1",
        "agent_key": "weak_auto_compact_ui_token_stack_safe_candidate",
        "parent_agent_key": "weak_auto_token_surface_semantic_boundary",
        "text_subfamily": "weak_auto_token_surface_semantic_delta",
        "shadow_action": "would_close_reopened_auto_confirmation_for_compact_ui_token_stack",
    },
    "weak_auto_short_exact_clean_ui": {
        "policy_name": "weak_auto_short_exact_clean_ui_shadow_v1",
        "agent_key": "weak_auto_short_exact_clean_ui_safe_candidate",
        "parent_agent_key": "weak_auto_short_exact_sampler",
        "text_subfamily": "weak_auto_tiny_exact",
        "shadow_action": "would_close_reopened_auto_confirmation_for_short_exact_clean_ui",
    },
}

SPANISH_RESIDUE_WORDS = {
    "abandonara",
    "abandonaras",
    "decidio",
    "decidiste",
    "despide",
    "despides",
    "entrega",
    "entregas",
    "extendio",
    "extendiste",
    "gana",
    "ganas",
    "gasta",
    "gastas",
    "nadie",
    "pierde",
    "pierdes",
    "posee",
    "posees",
    "retengo",
    "retiene",
    "resulta",
    "resultas",
    "tengo",
    "tiene",
    "titulo",
    "título",
    "tuyo",
    "tuya",
    "señor",
    "senor",
}


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def latest_diagnostic_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_diagnostic_runs
        WHERE finished_at IS NOT NULL
          AND total_items > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed auto-confirmation text diagnostic run found.")
    return int(row["id"])


def latest_specialist_audit_run_id(conn) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_specialist_audit_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def report_paths(settings: dict[str, Any], *, agent_key: str) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_shadow_policy_{agent_key}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_rows(conn, *, diagnostic_run_id: int, text_subfamily: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            output.portuguese_text,
            confirmation.confirmed_text,
            decision.id AS review_decision_id,
            decision.evidence_label,
            decision.decision AS review_decision,
            decision.corrected_text AS reviewed_corrected_text
        FROM auto_confirmation_reopen_text_diagnostic_items item
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
        LEFT JOIN auto_confirmation_reopen_text_review_decisions decision
          ON decision.id = (
              SELECT d.id
              FROM auto_confirmation_reopen_text_review_decisions d
              WHERE d.diagnostic_item_id = item.id
              ORDER BY d.updated_at DESC, d.id DESC
              LIMIT 1
          )
        WHERE item.run_id = ?
          AND item.text_subfamily = ?
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        (diagnostic_run_id, text_subfamily),
    ).fetchall()
    return [dict(row) for row in rows]


def has_spanish_residue(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", lowered) for word in SPANISH_RESIDUE_WORDS)


def visible_text_outside_ck3_tokens(text: str) -> str:
    visible = re.sub(r"\[[^\]]*\]", "", text)
    visible = re.sub(r"\$[^$]*\$", "", visible)
    visible = re.sub(r"@[A-Za-z0-9_]+!", "", visible)
    visible = re.sub(r"#[A-Za-z0-9_]+", "", visible)
    visible = visible.replace("#!", "")
    visible = visible.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")
    return visible


def has_visible_letters_outside_tokens(text: str) -> bool:
    return re.search(r"[A-Za-zÀ-ÿ]", visible_text_outside_ck3_tokens(text)) is not None


def has_embedded_visible_token_literals(text: str) -> bool:
    return any(marker in text for marker in ("Select_CString(", "LocalPlayerString(", "Concept(", "Glossary("))


def has_spanish_custom_localization_helper(text: str) -> bool:
    return re.search(r"\.Custom\(\s*'ES[_A-Za-z0-9]", text) is not None


def is_spanish_custom_localization_definition(row: dict[str, Any]) -> bool:
    return (
        str(row.get("relative_path") or "").startswith("custom_localization/es_custom_loc")
        or str(row.get("source_key") or "").startswith("Loc_ES_")
    )


def is_hold_court_aptitude_pattern(row: dict[str, Any], text: str) -> bool:
    key = row["source_key"]
    return (
        row["relative_path"].startswith("event_localization/hold_court_events/")
        and key.endswith("aptitude_tooltip")
        and "Select_CString( candidate.IsLocalPlayer" in text
        and "'Minha', 'Sua'" in text
        and "[aptitude|lE]" in text
        and "GetCourtPositionAptitude" in text
    )


def is_short_ui_relation_score_pattern(row: dict[str, Any], text: str) -> bool:
    return (
        row["relative_path"] == "diarchies/diarchies_l_spanish.yml"
        and row["source_key"].startswith("diarch_succession_score.")
        and row["text_subfamily"] == POLICIES["short_ui_relation_score"]["text_subfamily"]
        and int(row.get("select_cstring_count") or 0) >= 1
        and "Select_CString" in text
        and int(row.get("issue_count") or 0) == 0
    )


def is_short_tooltip_artifact_honor_reason_pattern(row: dict[str, Any], text: str) -> bool:
    return (
        row["relative_path"] == "interactions_l_spanish.yml"
        and row["source_key"] == "ARTIFACT_HONOR_REASON"
        and row["text_subfamily"] == POLICIES["short_tooltip_artifact_honor_reason"]["text_subfamily"]
        and int(row.get("select_cstring_count") or 0) == 2
        and int(row.get("issue_count") or 0) == 0
        and "recipient.IsLocalPlayer" in text
        and "recipient.GetShortUINameNoTooltip" in text
        and "$VALUE|=+0$" in text
        and "honra" in text.lower()
    )


def is_weak_auto_static_token_only_pattern(row: dict[str, Any], text: str) -> bool:
    return (
        row["text_subfamily"] == POLICIES["weak_auto_static_token_only"]["text_subfamily"]
        and int(row.get("issue_count") or 0) == 0
        and not has_visible_letters_outside_tokens(text)
        and not has_visible_letters_outside_tokens(row.get("english_text") or "")
        and not has_visible_letters_outside_tokens(row.get("spanish_text") or "")
        and not has_embedded_visible_token_literals(text)
        and not has_spanish_custom_localization_helper(text)
        and not is_spanish_custom_localization_definition(row)
    )


def is_weak_auto_embedded_possessive_runtime_pattern(row: dict[str, Any], text: str) -> bool:
    key = row["source_key"]
    return (
        row["relative_path"] == "diarchies/diarchies_l_spanish.yml"
        and row["text_subfamily"] == POLICIES["weak_auto_embedded_possessive_runtime"]["text_subfamily"]
        and int(row.get("issue_count") or 0) == 0
        and int(row.get("select_cstring_count") or 0) >= 1
        and "ROOT.Char.GetLiege.IsLocalPlayer" in text
        and (
            key.startswith("diarch_succession_score.")
            or key == "diarch_loyalty_score.regency.tyranny"
        )
        and (
            "do seu senhor" in text
            or "LIEGE_POSSESSIVE" in text
        )
        and any(marker in text for marker in ("'seu'", "'sua'", "'seu/sua'"))
        and not has_spanish_custom_localization_helper(text)
        and not has_spanish_residue(text)
    )


def is_weak_auto_compact_ui_token_stack_pattern(row: dict[str, Any], text: str) -> bool:
    return (
        row["text_subfamily"] == POLICIES["weak_auto_compact_ui_token_stack"]["text_subfamily"]
        and int(row.get("issue_count") or 0) == 0
        and text.strip() not in {"", "!"}
        and "ES_DelDela" not in text
        and not has_spanish_residue(text)
    )


def is_weak_auto_short_exact_clean_ui_pattern(row: dict[str, Any], text: str) -> bool:
    path = str(row.get("relative_path") or "")
    key = str(row.get("source_key") or "")
    normalized = text.casefold()
    if row["text_subfamily"] != POLICIES["weak_auto_short_exact_clean_ui"]["text_subfamily"]:
        return False
    if int(row.get("issue_count") or 0) != 0:
        return False
    if int(row.get("word_count") or 0) > 3:
        return False
    if not has_visible_letters_outside_tokens(text):
        return False
    if has_spanish_residue(text):
        return False
    if has_spanish_custom_localization_helper(text) or is_spanish_custom_localization_definition(row):
        return False
    if path.startswith("custom_localization/es_custom_loc"):
        return False
    if path.startswith("titles_cultural_names/") or path == "titles_cultural_names_l_spanish.yml":
        return False
    if path.startswith("historical_characters") or key.startswith(("dynn_", "bookmark_")):
        return False
    if "sua pessoa" in normalized or "ahem" in normalized or "abland" in normalized:
        return False
    if path == "titles_l_spanish.yml" and any(word in normalized for word in (" sur", " norte", " este", " oeste")):
        return False
    if key == "diarch_legal_meddling_interaction":
        return False
    return True


def row_matches_policy(row: dict[str, Any], *, policy_key: str, text: str) -> bool:
    if policy_key == "hold_court_aptitude_tooltip":
        return is_hold_court_aptitude_pattern(row, text)
    if policy_key == "short_ui_relation_score":
        return is_short_ui_relation_score_pattern(row, text)
    if policy_key == "short_tooltip_artifact_honor_reason":
        return is_short_tooltip_artifact_honor_reason_pattern(row, text)
    if policy_key == "weak_auto_static_token_only":
        return is_weak_auto_static_token_only_pattern(row, text)
    if policy_key == "weak_auto_embedded_possessive_runtime":
        return is_weak_auto_embedded_possessive_runtime_pattern(row, text)
    if policy_key == "weak_auto_compact_ui_token_stack":
        return is_weak_auto_compact_ui_token_stack_pattern(row, text)
    if policy_key == "weak_auto_short_exact_clean_ui":
        return is_weak_auto_short_exact_clean_ui_pattern(row, text)
    raise ValueError(f"Unknown text shadow policy: {policy_key}")


def evaluate_row(row: dict[str, Any], *, policy_key: str, shadow_action: str) -> dict[str, Any]:
    text = row.get("confirmed_text") or row.get("portuguese_text") or ""
    reasons: list[str] = []
    pattern_match = row_matches_policy(row, policy_key=policy_key, text=text)
    positive_evidence = row.get("evidence_label") == "positive_evidence"
    negative_evidence = row.get("evidence_label") == "negative_boundary"
    issue_count = int(row.get("issue_count") or 0)
    spanish_residue = has_spanish_residue(text)

    if pattern_match:
        reasons.append(f"{policy_key}_pattern")
    if positive_evidence:
        reasons.append("positive_review_evidence")
    if negative_evidence:
        reasons.append("negative_review_evidence")
    if issue_count:
        reasons.append("issue_signal_present")
    if spanish_residue:
        reasons.append("spanish_residue_seen")

    if not pattern_match:
        status = "blocked_by_shadow_guard"
        action = "keep_manual_review"
        block_reason = "pattern_mismatch"
    elif not positive_evidence:
        status = "blocked_by_shadow_guard"
        action = "keep_manual_review"
        block_reason = "missing_positive_evidence"
    elif negative_evidence:
        status = "blocked_by_shadow_guard"
        action = "keep_manual_review"
        block_reason = "negative_evidence"
    elif issue_count:
        status = "blocked_by_shadow_guard"
        action = "keep_manual_review"
        block_reason = "issue_signal_present"
    elif spanish_residue:
        status = "blocked_by_shadow_guard"
        action = "keep_manual_review"
        block_reason = "spanish_residue_seen"
    else:
        status = "shadow_ready"
        action = shadow_action
        block_reason = ""

    return {
        **row,
        "shadow_status": status,
        "shadow_action": action,
        "block_reason": block_reason,
        "pattern_match": 1 if pattern_match else 0,
        "positive_evidence": 1 if positive_evidence else 0,
        "negative_evidence": 1 if negative_evidence else 0,
        "current_confirmed_text_hash": sha256_text(text),
        "shadow_reasons": reasons,
    }


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    diagnostic_run_id: int,
    audit_run_id: int | None,
    policy_name: str,
    agent_key: str,
    parent_agent_key: str,
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> None:
    fieldnames = [
        "shadow_item_id",
        "diagnostic_item_id",
        "review_decision_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "agent_key",
        "text_subfamily",
        "shadow_status",
        "shadow_action",
        "block_reason",
        "pattern_match",
        "positive_evidence",
        "negative_evidence",
        "issue_count",
        "select_cstring_count",
        "concept_link_count",
        "spanish_literal_hint_count",
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
                "reasons": row.get("shadow_reasons"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    status_counts = Counter(row["shadow_status"] for row in rows)
    block_counts = Counter(row["block_reason"] for row in rows if row["block_reason"])
    lines = [
        "Auto-confirmation text shadow policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {policy_name}",
        "Policy status: shadow",
        f"Agent: {agent_key}",
        f"Parent agent: {parent_agent_key}",
        f"Shadow run id: {run_id}",
        f"Diagnostic run id: {diagnostic_run_id}",
        f"Specialist audit run id: {audit_run_id or ''}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Rows inspected: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in status_counts.most_common()],
        "",
        "Blocked reasons:",
        *([f"- {key}: {value:,}" for key, value in block_counts.most_common()] or ["- none"]),
        "",
        "Ready sample:",
    ]
    ready = [row for row in rows if row["shadow_status"] == "shadow_ready"]
    for row in ready[:20]:
        lines.extend(
            [
                f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  action={row['shadow_action']}; evidence={row.get('evidence_label')}",
            ]
        )
    if not ready:
        lines.append("- none")
    lines.extend(["", "Blocked sample:"])
    blocked = [row for row in rows if row["shadow_status"] != "shadow_ready"]
    for row in blocked[:20]:
        lines.extend(
            [
                f"- {row['block_reason']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  reasons={', '.join(row['shadow_reasons'])}",
                f"  confirmed={short(row.get('confirmed_text'))}",
            ]
        )
    if not blocked:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- This is shadow validation only.",
            "- It does not change confirmations, train models, promote production policy, or write output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, diagnostic_run_id: int | None = None, policy_key: str = DEFAULT_POLICY_KEY) -> dict[str, Any]:
    if policy_key not in POLICIES:
        expected = ", ".join(sorted(POLICIES))
        raise ValueError(f"Unknown policy_key={policy_key!r}. Expected one of: {expected}")
    policy = POLICIES[policy_key]
    policy_name = policy["policy_name"]
    agent_key = policy["agent_key"]
    parent_agent_key = policy["parent_agent_key"]
    text_subfamily = policy["text_subfamily"]
    shadow_action = policy["shadow_action"]
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_diagnostic_run_id = diagnostic_run_id or latest_diagnostic_run_id(conn)
        audit_run_id = latest_specialist_audit_run_id(conn)
        raw_rows = fetch_rows(conn, diagnostic_run_id=selected_diagnostic_run_id, text_subfamily=text_subfamily)
        rows = [
            evaluate_row(row, policy_key=policy_key, shadow_action=shadow_action)
            for row in raw_rows
        ]
        status_counts = Counter(row["shadow_status"] for row in rows)
        positive_count = sum(row["positive_evidence"] for row in rows)
        negative_count = sum(row["negative_evidence"] for row in rows)
        txt_path, csv_path, jsonl_path = report_paths(settings, agent_key=agent_key)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_shadow_policy_runs (
                rule_version,
                policy_name,
                policy_status,
                agent_key,
                parent_agent_key,
                diagnostic_run_id,
                specialist_audit_run_id,
                total_candidates,
                shadow_ready_count,
                blocked_count,
                positive_evidence_count,
                negative_evidence_count,
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
                policy_name,
                "shadow",
                agent_key,
                parent_agent_key,
                selected_diagnostic_run_id,
                audit_run_id,
                len(rows),
                status_counts["shadow_ready"],
                len(rows) - status_counts["shadow_ready"],
                positive_count,
                negative_count,
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO auto_confirmation_reopen_text_shadow_policy_items (
                    run_id,
                    diagnostic_run_id,
                    diagnostic_item_id,
                    review_decision_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    agent_key,
                    text_subfamily,
                    shadow_status,
                    shadow_action,
                    block_reason,
                    pattern_match,
                    positive_evidence,
                    negative_evidence,
                    issue_count,
                    select_cstring_count,
                    concept_link_count,
                    spanish_literal_hint_count,
                    current_confirmed_text_hash,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_diagnostic_run_id,
                    row["id"],
                    row.get("review_decision_id"),
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    agent_key,
                    row["text_subfamily"],
                    row["shadow_status"],
                    row["shadow_action"],
                    row["block_reason"],
                    row["pattern_match"],
                    row["positive_evidence"],
                    row["negative_evidence"],
                    int(row.get("issue_count") or 0),
                    int(row.get("select_cstring_count") or 0),
                    int(row.get("concept_link_count") or 0),
                    int(row.get("spanish_literal_hint_count") or 0),
                    row["current_confirmed_text_hash"],
                    json.dumps(row["shadow_reasons"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            row["shadow_item_id"] = int(item_cursor.lastrowid)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            diagnostic_run_id=selected_diagnostic_run_id,
            audit_run_id=audit_run_id,
            policy_name=policy_name,
            agent_key=agent_key,
            parent_agent_key=parent_agent_key,
            rows=rows,
            started_at=started_at,
        )
        conn.commit()

    print("[auto_confirmation_reopen_text_shadow_policy] Shadow policy generated")
    print(f"[auto_confirmation_reopen_text_shadow_policy] Policy: {policy_key}")
    print(f"[auto_confirmation_reopen_text_shadow_policy] Run id: {run_id}")
    print(f"[auto_confirmation_reopen_text_shadow_policy] Diagnostic run id: {selected_diagnostic_run_id}")
    print(f"[auto_confirmation_reopen_text_shadow_policy] Rows inspected: {len(rows):,}")
    for key, value in status_counts.most_common():
        print(f"[auto_confirmation_reopen_text_shadow_policy] {key}: {value:,}")
    print("[auto_confirmation_reopen_text_shadow_policy] Apply allowed: 0")
    print(f"[auto_confirmation_reopen_text_shadow_policy] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_shadow_policy] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_shadow_policy] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "diagnostic_run_id": selected_diagnostic_run_id,
        "rows": len(rows),
        "shadow_ready": status_counts["shadow_ready"],
        "blocked": len(rows) - status_counts["shadow_ready"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build shadow-only policy for safe auto-confirmation text subpatterns.")
    parser.add_argument("--diagnostic-run-id", type=int, default=None)
    parser.add_argument("--policy", choices=sorted(POLICIES), default=DEFAULT_POLICY_KEY)
    args = parser.parse_args()
    main(diagnostic_run_id=args.diagnostic_run_id, policy_key=args.policy)
