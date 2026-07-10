from __future__ import annotations

import argparse
import difflib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_narrative_plain_light_corrected_preview_v1"
PACKET_JSONL = Path("reports/20260703_005406_073466_release_readiness_narrative_plain_light_human_packet.jsonl")


PROPOSED_TEXT = {
    113374: "As [scales_of_power|lE] foram ativadas e podem ser inclinadas para desbloquear mais [diarch_powers|lE]",
    79601: "Você já [Concept('power_sharing', 'compartes el poder')|E] e não precisa fazer mudanças para acomodar o peso extra.",
    112693: "#X Nenhuma de suas [holdings|lE] pode sustentar uma [holding|lE] [Concept( 'city', 'urbana' )|E]#!",
    112699: "#X Nenhuma de suas [holdings|lE] pode sustentar uma [holding|lE] [Concept( 'temple', 'consagrada' )|E]#!",
    70298: "#P [allied_count|0V] [vassals|lE] [Concept( 'allied', 'aliados' )|E] ou da mesma [house|E] se juntarão a você#!",
    104908: "Os [hooks|lE] de #EMP obrigação#! reduzem os salários dos [Concept('court_position', 'puestos de la corte')|E] em #P -50%#! [gold_i]",
    30863: "Recebe um [modifier|lE] que aumenta ligeiramente sua [skill|lE] pertinente e [opinion|lE] sobre #high você#! durante #high 5 anos#!",
    30864: "Ganha um [modifier|lE] que aumenta ligeiramente sua maior [skill|lE] e [opinion|lE] sobre #high você#! durante #high 5 anos#!",
    31045: "$every_detractor$ ganha #P 10#! de [opinion|lE] sobre você #weak (respeito) (-1,20/ano)#!",
    55019: "#EMP Como isso excederia sua capacidade de [provisions|lE], as [provisions_overflow_value|V0] @provisions_icon! extras serão vendidas no mercado local por [provisions_transformed_to_gold_value|V0] @gold_icon!#!",
    65282: "#EMP Revistar o cadáver; não é como se ele fosse precisar de algo agora#!",
    66438: "As [holdings|lE] pelas quais [Concept('migrate', 'migres')|E] fornecerão [gold|lE] @gold_icon! e [herd|lE] @herd_icon!, mas você perderá [opinion|lE] e [cultural_acceptance|lE] com o [holder|lE] delas",
    66439: "As [holdings|lE] pelas quais [Concept('migrate', 'migres')|E] fornecerão [gold|lE] @gold_icon! e [herd|lE] @herd_icon!, mas você perderá [opinion|lE] com o [holder|lE] delas",
    70297: "#P [soryo_count|0V] [vassals|lE] [Concept('feudal', 'feudales')|E] se juntarão a você#!",
    70373: "@warning_icon! #X O [ceremonial_regent|lE] é [ritsuryo|lE] e considerará isto um [crime|lE] [crime_i|E]#!",
    102046: "#EMP O momento chegará...#!",
    102719: "#EMP ...e você bate a cabeça nesta rocha...#!",
    112779: "#X Seu [vassal_contract|lE] foi alterado recentemente demais#!",
    117478: "No acampamento deste personagem, as pessoas estão #EMP muito#! alegres; só não muito focadas.",
    123559: "@warning_icon! #X Encerra qualquer [scheme|lE] pessoal em andamento#!.",
    130189: "#F Tento chamar sua atenção de maneira discreta#!",
    132024: "Todo [county|lE] na [Glossary( 'Grande Estrada do Norte ', 'GNR_GLOSS' )]",
    132025: "Todos os [counties|lE] controlados por anglo-saxões ao longo da [Glossary( 'Grande Estrada do Norte ', 'GNR_GLOSS' )]:",
    132026: "Todos os [counties|lE] controlados por normandos ao longo da [Glossary( 'Grande Estrada do Norte ', 'GNR_GLOSS' )]:",
    133040: "Este personagem #EMP detesta#! ser observado.",
    133273: "Aqui embaixo #EMP há#! fontes alternativas de comida...",
    30464: "A coroa é colocada sobre sua testa.",
    31402: "#weak #EMP Isso aconteceu devido à forte presença de [coronation_detractors|lE] em sua [coronation|lE]#!#!",
    32380: "Cuidarei disso. Agora, deixe que eu #EMP cuide#! de #EMP você...#!",
    33718: "\\\"Cala a boca! Eu te odeio!\\\"",
    37839: "\\\"Meu amor... Não vou te desapontar.\\\"",
    42001: "Se vocês têm #EMP certeza#! de que isso vai funcionar...",
    42196: "\\\"Vencer não vai te render amigos.\\\"",
    42674: "A pé, sim. Mas #EMP com#! lanças.",
    45276: "Tentar me colocar no meio pode ser arriscado, mas devo fazer #EMP algo#!...",
    45336: "Foi quase #EMP fácil demais#!...",
}


TOKEN_POLICY_BLOCK_IDS = {79601, 112693, 112699, 104908, 66438, 66439, 70297, 132024, 132025, 132026}
CONTEXT_BLOCK_IDS = {65282, 130189, 30464}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only diff preview for narrative plain/light corrected_text rows.")
    parser.add_argument("--packet-jsonl", type=Path, default=PACKET_JSONL)
    return parser.parse_args()


def read_packet(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("suggested_decision") == "corrected_text":
                rows.append(row)
    return rows


def token_integrity_ok(old: str, new: str) -> bool:
    return Counter(protected_tokens(old)) == Counter(protected_tokens(new))


def structure_integrity_ok(old: str, new: str) -> bool:
    markers = ["#T", "#X", "#P", "#N", "#EMP", "#weak", "#WEAK", "#!", "@warning_icon!", "@provisions_icon!", "@gold_icon!", "@herd_icon!"]
    if any((marker in old) != (marker in new) for marker in markers):
        return False
    for char in ["[", "]", "$"]:
        if old.count(char) != new.count(char):
            return False
    return True


def classify_issues(row: dict[str, Any], corrected: str, blocked: bool) -> list[dict[str, str]]:
    families = [part for part in str(row.get("issue_families") or "").split(",") if part]
    kinds = [part for part in str(row.get("issue_kinds") or "").split(",") if part]
    severities = ["high"] * int(row.get("high_issue_count") or 0)
    if not severities:
        severities = ["medium"] * max(1, int(row.get("open_issue_count") or 1))
    out: list[dict[str, str]] = []
    max_len = max(len(families), len(kinds), len(severities))
    old = row.get("output_text") or ""
    residual_after = any(word in corrected.lower() for word in [" años", " año", " respeto", " mayor", " esto ", " tu ", " ti", "migres", "feudales", "puestos de la corte", "compartes el poder"])
    for idx in range(max_len):
        family = families[idx] if idx < len(families) else (families[-1] if families else "unknown")
        kind = kinds[idx] if idx < len(kinds) else (kinds[-1] if kinds else "unknown")
        if blocked:
            issue_class = "needs_human_context" if "context" in kind or "gender" in kind else "still_open_after_corrected_text"
        elif "spanish" in family or "spanish" in kind or residual_after:
            issue_class = "resolved_by_corrected_text" if not residual_after and canonical_localization_text(old) != canonical_localization_text(corrected) else "still_open_after_corrected_text"
        elif "high_issue" in family or "semantic" in family or "short_label" in family:
            issue_class = "resolved_by_corrected_text" if canonical_localization_text(old) != canonical_localization_text(corrected) else "unrelated_or_superseded"
        else:
            issue_class = "unrelated_or_superseded"
        out.append({"issue_family": family, "issue_kind": kind, "issue_class": issue_class})
    return out


def diff_lines(old: str, new: str) -> list[str]:
    return list(difflib.unified_diff([old], [new], fromfile="current_output", tofile="proposed_corrected", lineterm=""))


def build_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        segment_id = int(row["segment_id"])
        current = row.get("output_text") or ""
        corrected = PROPOSED_TEXT.get(segment_id, "")
        reasons: list[str] = []
        if not corrected:
            reasons.append("missing_corrected_text")
        tok_ok = token_integrity_ok(current, corrected) if corrected else False
        struct_ok = structure_integrity_ok(current, corrected) if corrected else False
        canon_change = canonical_localization_text(current) != canonical_localization_text(corrected) if corrected else False
        if not tok_ok:
            reasons.append("token_integrity_failed")
        if not struct_ok:
            reasons.append("structure_integrity_failed")
        if not canon_change:
            reasons.append("canonical_l10n_no_change")
        if segment_id in TOKEN_POLICY_BLOCK_IDS:
            reasons.append("concept_literal_or_token_surface_needs_policy")
        if segment_id in CONTEXT_BLOCK_IDS:
            reasons.append("needs_human_context_for_pronoun_or_actor")
        blocked = bool(reasons)
        records.append(
            {
                "source": SOURCE,
                "record_type": "corrected_text_diff_preview_item",
                "segment_id": segment_id,
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "release_class": row.get("release_class"),
                "token_surface": row.get("token_surface"),
                "packet_risk_type": row.get("packet_risk_type"),
                "open_issue_count": int(row.get("open_issue_count") or 0),
                "high_issue_count": int(row.get("high_issue_count") or 0),
                "issue_families": row.get("issue_families"),
                "issue_kinds": row.get("issue_kinds"),
                "source_text": row.get("spanish_text"),
                "english_text": row.get("english_text"),
                "current_output_text": current,
                "confirmed_text": row.get("confirmed_text"),
                "proposed_corrected_text": corrected,
                "token_integrity_ok": tok_ok,
                "structure_integrity_ok": struct_ok,
                "canonical_l10n_changes": canon_change,
                "issue_classifications": classify_issues(row, corrected, blocked),
                "diff_preview": diff_lines(current, corrected) if corrected else [],
                "status": "ready_for_protected_apply" if not reasons else "blocked",
                "block_reasons": reasons,
                "candidate_generation_count": 0,
                "apply_count": 0,
                "learning_ingest_count": 0,
                "issue_closure_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    return records


def build_summary(records: list[dict[str, Any]], packet_jsonl: Path) -> dict[str, Any]:
    ready = [record for record in records if record["status"] == "ready_for_protected_apply"]
    blocked = [record for record in records if record["status"] == "blocked"]
    issue_classes = Counter(
        issue["issue_class"]
        for record in records
        for issue in record.get("issue_classifications", [])
    )
    return {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_diff_preview",
        "input_jsonl": str(packet_jsonl),
        "record_count": len(records),
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "eligible_for_protected_apply_segment_ids": [record["segment_id"] for record in ready],
        "blocked_segment_ids": [record["segment_id"] for record in blocked],
        "block_reason_counts": dict(Counter(reason for record in blocked for reason in record["block_reasons"]).most_common()),
        "token_integrity_ok_count": sum(1 for record in records if record["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for record in records if record["structure_integrity_ok"]),
        "canonical_l10n_changes_count": sum(1 for record in records if record["canonical_l10n_changes"]),
        "issue_class_counts": dict(issue_classes.most_common()),
        "risk_type_counts": dict(Counter(record["packet_risk_type"] for record in records).most_common()),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Only apply the ready_for_protected_apply subset after human approval. Keep token-policy and context blocked rows out of apply."
        ),
    }


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_narrative_plain_light_corrected_preview"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# Narrative Plain/Light Corrected Text Diff Preview",
        "",
        f"- record_count: {summary['record_count']}",
        f"- ready_count: {summary['ready_count']}",
        f"- blocked_count: {summary['blocked_count']}",
        f"- block_reason_counts: `{json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- issue_class_counts: `{json.dumps(summary['issue_class_counts'], ensure_ascii=False, sort_keys=True)}`",
        "- candidate_generation_count: 0",
        "- apply_count: 0",
        "- learning_ingest_count: 0",
        "- issue_closure_count: 0",
        "- lifecycle_count: 0",
        "- segment_state_count: 0",
        "- reindex_count: 0",
        "- production_full_count: 0",
        "",
    ]
    for record in records:
        lines.extend(
            [
                f"## Segment {record['segment_id']} - {record['source_key']}",
                "",
                f"- status: `{record['status']}`",
                f"- path: `{record['relative_path']}`",
                f"- token_integrity_ok: `{record['token_integrity_ok']}`",
                f"- structure_integrity_ok: `{record['structure_integrity_ok']}`",
                f"- canonical_l10n_changes: `{record['canonical_l10n_changes']}`",
                f"- open/high issues: `{record['open_issue_count']}/{record['high_issue_count']}`",
                f"- block_reasons: `{json.dumps(record['block_reasons'], ensure_ascii=False)}`",
                "",
                "**Current output**",
                "",
                "```text",
                record["current_output_text"],
                "```",
                "",
                "**Proposed corrected_text**",
                "",
                "```text",
                record["proposed_corrected_text"],
                "```",
                "",
                "**Diff**",
                "",
                "```diff",
                *record["diff_preview"],
                "```",
                "",
                "**Issue classification**",
                "",
                "```json",
                json.dumps(record["issue_classifications"], ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    rows = read_packet(args.packet_jsonl)
    records = build_records(rows)
    summary = build_summary(records, args.packet_jsonl)
    md_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"eligible_for_protected_apply_segment_ids={summary['eligible_for_protected_apply_segment_ids']}")
    print(f"blocked_segment_ids={summary['blocked_segment_ids']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
