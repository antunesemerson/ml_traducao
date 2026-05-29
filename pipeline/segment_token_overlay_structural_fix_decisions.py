from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import short, structural_tokens
from segment_token_overlay_review_queue import DEFAULT_ACTIVE_GATE_KEY, active_gate_overlay_run_id


RULE_VERSION = "segment_token_overlay_structural_fix_decisions_v1"


CURATED_STRUCTURAL_FIXES: dict[int, str] = {
    73905: (
        "As $game_concept_silk_road_innovations$ são [Concept('innovations', 'inovações')|E] que se espalham para o oeste ao longo da [silk_road_situation|E].\n\n"
        "A cada [GetDefine('NSilkRoad', 'INNOVATION_TRAVEL_DURATION')|0V] anos, uma $game_concept_silk_road_innovation$ descoberta pela [culture|lE] Han será atribuída aleatoriamente à [situation_sub_region|lE] da China, com todas as outras $game_concept_silk_road_innovations$ se movendo para o oeste ao longo da $game_concept_silk_road$.\n\n"
        "As $game_concept_cultures$ com a [tradition|lE] [GetCultureTradition('tradition_sinophilic').GetName|l] têm acesso a todas as inovações descobertas pela $game_concept_culture$ Han.\n\n"
        "Os [rulers|lE] fora da $game_concept_silk_road$ podem tentar trazer conhecimento de volta à sua [culture|lE] por meio da [decision|lE] [GetDecisionWithKey('visit_silk_road_market_decision').GetName|V]. Se forem bem-sucedidos, a $game_concept_silk_road_innovation$ poderá ser descoberta como qualquer outra $game_concept_innovation$ ao retornarem."
    ),
    107991: (
        "Que canalha[founder.Custom('ES_OA')]! Que miserável! Ess[founder.Custom('ES_EA')] #EMP [founder.Custom2('InsultNoun', ROOT.Char)]#! [founder.Custom2('InsultAdjective', ROOT.Char)], [founder.Custom2('InsultAdjective', ROOT.Char)]!\n\n"
        "Embora os reinos de $k_england$ e o $k_danelaw$ estivessem divididos há tempo demais, #EMP todos#! sabiam que um dia seriam reunidos.\n\n"
        "Até hoje.\n\n"
        "Em uma declaração grandiosa e pomposa, [founder.Custom('ES_ElLa')] [founder.GetTitledFirstName|l] renunciou formalmente a qualquer direito de controlar $k_england$ e, por tabela, tirou toda a esperança de reunir facilmente meu reino com as terras do $k_danelaw$!"
    ),
    243892: (
        "O $murder$ é uma [hostile_scheme|lE] que tem como objetivo matar seu alvo. É uma [Concept( 'secret_scheme', 'conjura secreta' )|E] baseada em [intrigue|lE] e pode recrutar [agents|lE].\n\n"
        "Um assassinato bem-sucedido resultará na [Concept( 'character_death_death', 'morte' )|E] do [scheme_target|lE]. Um assassinato que permanecer em segredo dará ao [Concept( 'scheme_owner', 'líder da conjura' )|E] um [secret|lE] de assassinato.\n\n"
        "Ser descoberto tentando assassinar alguém, ou assassinando alguém com sucesso, é um [crime|El] grave."
    ),
    244089: (
        "A [scheme|lE] $overthrow_regent$ é uma [hostile_scheme|lE] que busca usurpar o posto de [regent|lE] de seu alvo [Concept( 'imprison', 'aprisionando-o' )|E]. É uma [Concept( 'secret_scheme', 'conjura secreta' )|E] baseada em [intrigue|lE] e permite recrutar [agents|lE].\n\n"
        "Se o golpe for bem-sucedido, o [scheme_target|lE] acabará aprisionado pelo [scheme_owner|lE] (se [Concept( 'landed', 'tiver terras' )|E]) ou pelo [liege|lE] (se não)."
    ),
}

RESTORE_OUTPUT_SEGMENT_IDS = {
    58751,
    286082,
}


def latest_overlay_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_token_policy_overlay_runs
        WHERE finished_at IS NOT NULL
          AND total_candidates > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete segment_token_policy_overlay_runs entry found.")
    return int(row["id"])


def fetch_rows(conn, *, overlay_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            oi.run_id AS overlay_run_id,
            oi.source_policy_run_id,
            oi.source_policy_item_id AS policy_item_id,
            oi.segment_id,
            oi.relative_path,
            oi.source_key,
            oi.source_line_number,
            oi.overlay_policy_bucket,
            oi.overlay_risk_level,
            i.missing_tokens_json,
            i.extra_tokens_json,
            i.issue_flags_json,
            s.spanish_text,
            sc.confirmed_text,
            o.portuguese_text AS output_text
        FROM segment_token_policy_overlay_items oi
        JOIN segment_token_policy_items i ON i.id = oi.source_policy_item_id
        JOIN source_segments s ON s.id = oi.segment_id
        JOIN segment_confirmations sc ON sc.segment_id = oi.segment_id
        LEFT JOIN output_segments o ON o.segment_id = oi.segment_id
        WHERE oi.run_id = ?
          AND oi.overlay_risk_level = 'critical'
          AND oi.overlay_policy_bucket = 'blocked_variable_or_icon_change'
        ORDER BY oi.relative_path, oi.source_line_number
        """,
        (overlay_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def validator_flags_mojibake(text: str) -> bool:
    issues = local_quality_validator.validate_text(text)["issues"]
    return any(issue.get("code") == "replacement_question_mark_mojibake" for issue in issues)


def choose_correction(row: dict[str, Any]) -> tuple[str, str, list[str]]:
    segment_id = int(row["segment_id"])
    output = row.get("output_text") or ""
    if segment_id in CURATED_STRUCTURAL_FIXES:
        return "manual_token_rewrite_required", CURATED_STRUCTURAL_FIXES[segment_id], ["curated_structural_rewrite"]
    if segment_id in RESTORE_OUTPUT_SEGMENT_IDS and output:
        return "manual_token_rewrite_required", output, ["restore_current_output_text"]
    return "defer_manual_review", "", ["no_curated_structural_fix_available"]


def classify(row: dict[str, Any]) -> dict[str, Any]:
    decision, corrected_text, reasons = choose_correction(row)
    current = row.get("confirmed_text") or ""
    output = row.get("output_text") or ""
    spanish = row.get("spanish_text") or ""
    curation_reasons = [f"rule:{RULE_VERSION}", *reasons]

    if decision == "manual_token_rewrite_required":
        if corrected_text == current:
            decision = "defer_manual_review"
            corrected_text = ""
            curation_reasons.append("corrected_text_same_as_current")
        elif not output:
            decision = "defer_manual_review"
            corrected_text = ""
            curation_reasons.append("missing_output_text_for_token_mirror")
        elif structural_tokens(output) != structural_tokens(spanish):
            decision = "defer_manual_review"
            corrected_text = ""
            curation_reasons.append("output_tokens_do_not_match_spanish_source_tokens")
        elif structural_tokens(corrected_text) != structural_tokens(output):
            decision = "defer_manual_review"
            corrected_text = ""
            curation_reasons.append("corrected_tokens_do_not_match_output_tokens")
        elif validator_flags_mojibake(corrected_text):
            decision = "defer_manual_review"
            corrected_text = ""
            curation_reasons.append("validator_still_flags_question_mark_mojibake")
        else:
            curation_reasons.append("corrected_tokens_match_output_and_source")

    return {
        **row,
        "decision": decision,
        "corrected_text": corrected_text,
        "curation_reasons": curation_reasons,
    }


def write_outputs(
    settings: dict[str, Any],
    *,
    overlay_run_id: int,
    rows: list[dict[str, Any]],
    source_mode: str,
    active_gate: dict[str, Any] | None,
) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    decisions_path = reports_dir / f"{timestamp}_segment_token_overlay_structural_fix_decisions.jsonl"
    report_path = reports_dir / f"{timestamp}_segment_token_overlay_structural_fix_decisions.txt"

    with decisions_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            if row["decision"] != "manual_token_rewrite_required":
                continue
            handle.write(
                json.dumps(
                    {
                        "policy_item_id": row["policy_item_id"],
                        "decision": row["decision"],
                        "corrected_text": row["corrected_text"],
                        "notes": "; ".join(row["curation_reasons"]),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    counts = Counter(row["decision"] for row in rows)
    lines = [
        "Segment token overlay structural fix decisions",
        f"Rule version: {RULE_VERSION}",
        f"Source mode: {source_mode}",
        f"Overlay run id: {overlay_run_id}",
        f"Active gate: {active_gate.get('gate_key') if active_gate else 'none'}",
        f"Active checkpoint id: {active_gate.get('active_checkpoint_id') if active_gate else 'none'}",
        f"Auto-apply allowed: {active_gate.get('auto_apply_allowed') if active_gate else 'n/a'}",
        f"Rows inspected: {len(rows)}",
        f"Decision rows emitted: {counts.get('manual_token_rewrite_required', 0)}",
        f"Decisions JSONL: {decisions_path}",
        "",
        "Decision counts:",
        *[f"- {key}: {value}" for key, value in counts.most_common()],
        "",
        "Preview:",
    ]
    for row in rows:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['decision']} | {row['relative_path']}:{row['source_line_number']} | "
                    f"{row['source_key']}"
                ),
                f"  reasons: {json.dumps(row['curation_reasons'], ensure_ascii=False)}",
                f"  current:   {short(row['confirmed_text'], 300)}",
                f"  corrected: {short(row['corrected_text'], 300)}",
            ]
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path, decisions_path


def resolve_overlay_run(
    conn,
    *,
    overlay_run_id: int | None,
    use_active_gate: bool,
    gate_key: str,
) -> tuple[int, str, dict[str, Any] | None]:
    if overlay_run_id is not None and use_active_gate:
        raise RuntimeError("--overlay-run-id and --use-active-composite-gate are mutually exclusive.")
    if use_active_gate:
        selected_overlay_run_id, active_gate = active_gate_overlay_run_id(conn, gate_key=gate_key)
        return selected_overlay_run_id, "active_composite_gate", active_gate
    if overlay_run_id is not None:
        return overlay_run_id, "explicit_overlay_run", None
    return latest_overlay_run_id(conn), "latest_completed_overlay_run", None


def main(
    *,
    overlay_run_id: int | None = None,
    use_active_gate: bool = False,
    gate_key: str = DEFAULT_ACTIVE_GATE_KEY,
) -> None:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_overlay_run_id, source_mode, active_gate = resolve_overlay_run(
            conn,
            overlay_run_id=overlay_run_id,
            use_active_gate=use_active_gate,
            gate_key=gate_key,
        )
        raw_rows = fetch_rows(conn, overlay_run_id=selected_overlay_run_id)
    rows = [classify(row) for row in raw_rows]
    report_path, decisions_path = write_outputs(
        settings,
        overlay_run_id=selected_overlay_run_id,
        rows=rows,
        source_mode=source_mode,
        active_gate=active_gate,
    )
    counts = Counter(row["decision"] for row in rows)
    print("[segment_token_overlay_structural_fix_decisions] Decisions generated")
    print(f"[segment_token_overlay_structural_fix_decisions] Rule version: {RULE_VERSION}")
    print(f"[segment_token_overlay_structural_fix_decisions] Source mode: {source_mode}")
    print(f"[segment_token_overlay_structural_fix_decisions] Overlay run id: {selected_overlay_run_id}")
    if active_gate:
        print(f"[segment_token_overlay_structural_fix_decisions] Active gate: {active_gate['gate_key']}")
        print(f"[segment_token_overlay_structural_fix_decisions] Active checkpoint id: {active_gate['active_checkpoint_id']}")
        print("[segment_token_overlay_structural_fix_decisions] Auto-apply allowed: 0")
    print(f"[segment_token_overlay_structural_fix_decisions] Rows inspected: {len(rows)}")
    for key, value in counts.most_common():
        print(f"[segment_token_overlay_structural_fix_decisions] {key}: {value}")
    print(f"[segment_token_overlay_structural_fix_decisions] Report: {report_path}")
    print(f"[segment_token_overlay_structural_fix_decisions] Decisions JSONL: {decisions_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build curated structural rewrite decisions from the token overlay.")
    parser.add_argument("--overlay-run-id", type=int, default=None)
    parser.add_argument("--use-active-composite-gate", action="store_true")
    parser.add_argument("--gate-key", default=DEFAULT_ACTIVE_GATE_KEY)
    args = parser.parse_args()
    main(
        overlay_run_id=args.overlay_run_id,
        use_active_gate=args.use_active_composite_gate,
        gate_key=args.gate_key,
    )
