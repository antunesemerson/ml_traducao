from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_decision_post592_top15_decision_aid_v1"
DEFAULT_PACKET_JSONL = Path("reports/20260703_235712_724351_release_decision_post592_top15_human_packet.jsonl")
DEFAULT_PACKET_MD = Path("reports/20260703_235712_724351_release_decision_post592_top15_human_packet.md")

SUGGESTIONS: dict[int, tuple[str, str, str]] = {
    79601: (
        "corrected_text",
        "VocÃª jÃ¡ [Concept('power_sharing', 'compartilha o poder')|E] e nÃ£o precisa fazer mudanÃ§as para acomodar o peso extra.",
        "Literal de Concept em espanhol; inglÃªs confirma sentido de power sharing.",
    ),
    112693: (
        "corrected_text",
        "#X Nenhuma de suas [holdings|lE] pode comportar uma [holding|lE] [Concept( 'city', 'urbana' )|E]#!",
        "Texto inteiro estÃ¡ em espanhol; preservar Concept e tokens de holding.",
    ),
    112699: (
        "corrected_text",
        "#X Nenhuma de suas [holdings|lE] pode comportar uma [holding|lE] [Concept( 'temple', 'consagrada' )|E]#!",
        "Texto inteiro estÃ¡ em espanhol; preservar Concept e tokens de holding.",
    ),
    104908: (
        "corrected_text",
        "Os [hooks|lE] de #EMP obrigaÃ§Ã£o#! reduzem os salÃ¡rios dos [Concept('court_position', 'postos da corte')|E] em #P -50Â %#!Â [gold_i]",
        "ResÃ­duos em #EMP e literal de Concept; manter indicador de ouro e valor.",
    ),
    121866: (
        "corrected_text",
        "#weak pelas habilidades adequadas de seu cÃ´njuge.#!",
        "Frase curta totalmente em espanhol; inglÃªs confirma causalidade.",
    ),
    121868: (
        "corrected_text",
        "#weak pelas habilidades excepcionais de seu cÃ´njuge.#!",
        "Frase curta totalmente em espanhol; inglÃªs confirma causalidade.",
    ),
    124242: (
        "corrected_text",
        "VocÃª nÃ£o pode mais tomar a [decision|lE] #high $mpo_gok_world_conquest_decision$#!",
        "Texto curto em espanhol com token/variÃ¡vel simples preservÃ¡vel.",
    ),
    127935: (
        "corrected_text",
        "#F Um elogio sincero ajudaria meu esquema a avanÃ§ar, mas, se eu errar, poderia causar ofensa.#!",
        "Evita pronome de alvo ausente no texto espanhol; inglÃªs indica target, mas sem token no output.",
    ),
    132509: (
        "corrected_text",
        "#F Um fiel companheiro canino caminha ao seu lado, oferecendo proteÃ§Ã£o e um vÃ­nculo de lealdade que poucos humanos conseguem igualar.#!",
        "DescriÃ§Ã£o narrativa direta, sem tokens dinÃ¢micos.",
    ),
    132516: (
        "corrected_text",
        "#F Um felino orgulhoso lhe faz companhia, um caÃ§ador Ã¡gil que oferece afeto apenas em seus prÃ³prios termos.#!",
        "DescriÃ§Ã£o narrativa direta, sem tokens dinÃ¢micos.",
    ),
    132520: (
        "corrected_text",
        "#F Em sua luva pousa uma Ã¡guia caÃ§adora, sempre vigilante, sempre faminta, trazendo a morte vinda dos cÃ©us.#!",
        "DescriÃ§Ã£o narrativa direta, sem tokens dinÃ¢micos.",
    ),
    134297: (
        "corrected_text",
        "Os moradores daqui devem ter #EMP alguma coisa#!.",
        "Frase curta em espanhol; #EMP preservado.",
    ),
    30753: (
        "corrected_text",
        "#weak Este [modifier|lE] se torna negativo se este [county|lE] for roubado#!",
        "Tooltip curto com tokens simples; espanhol residual claro.",
    ),
    31096: (
        "corrected_text",
        "A [scheme|lE] terÃ¡ #P maior#! [success_chance|lE]",
        "Tooltip curto com tokens simples; traduz mayor preservando tag positiva.",
    ),
    31387: (
        "corrected_text",
        "@warning_icon! #X Isso encerrarÃ¡ a atividade#!",
        "Aviso curto sem dependÃªncia contextual.",
    ),
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only decision aid for post-592 top15 human packet.")
    parser.add_argument("--packet-jsonl", type=Path, default=DEFAULT_PACKET_JSONL)
    parser.add_argument("--packet-md", type=Path, default=DEFAULT_PACKET_MD)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def tokens(text: str) -> list[str]:
    return re.findall(r"\[[^\]]+\]|\$[^$\s]+\$|@[A-Za-z0-9_]+!|#[A-Za-z0-9_]+|#!", text or "")


def token_delta(current: str, suggested: str) -> str:
    current_tokens = tokens(current)
    suggested_tokens = tokens(suggested)
    if current_tokens == suggested_tokens:
        return "tokens_preserved_exact_signature"
    return "token_surface_changed_inside_expression_or_order_review_required"


def main() -> None:
    args = parse_args()
    rows = read_jsonl(args.packet_jsonl)
    records: list[dict[str, Any]] = []
    for row in rows:
        segment_id = int(row["segment_id"])
        suggested_decision, suggested_text, rationale = SUGGESTIONS.get(
            segment_id,
            ("needs_more_context", "", "Sem sugestÃ£o assistida segura."),
        )
        current_output = row.get("output_text") or ""
        record = {
            "schema_version": 1,
            "source": SOURCE,
            "segment_id": segment_id,
            "packet_index": row.get("packet_index"),
            "source_key": row.get("source_key"),
            "relative_path": row.get("relative_path"),
            "source_line_number": row.get("source_line_number"),
            "english_text": row.get("english_text"),
            "spanish_text": row.get("spanish_text"),
            "output_text": current_output,
            "confirmed_text": row.get("current_target_or_confirmed_text"),
            "open_issue_count": row.get("open_issue_count"),
            "high_issue_count": row.get("high_issue_count"),
            "issue_families": row.get("issue_families"),
            "issue_kinds": row.get("issue_kinds"),
            "tokens_present": tokens(current_output),
            "token_structure_risk": row.get("token_structure_risk"),
            "primary_risk": (
                "concept_literal_spanish_residue"
                if "concept_expression" in str(row.get("issue_kinds") or "")
                else "spanish_residue_visible"
            ),
            "assisted_suggestion_not_human_decision": True,
            "suggested_human_decision": suggested_decision,
            "suggested_corrected_text": suggested_text,
            "suggestion_rationale": rationale,
            "token_delta_assessment": token_delta(current_output, suggested_text) if suggested_text else "not_applicable",
            "human_decision_to_fill": "",
            "corrected_text_to_fill_if_accepted": "",
            "human_notes": "",
        }
        records.append(record)
    decision_counts = Counter(row["suggested_human_decision"] for row in records)
    token_delta_counts = Counter(row["token_delta_assessment"] for row in records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_decision_aid_not_human_decision",
        "input_packet_markdown": str(args.packet_md),
        "input_packet_jsonl": str(args.packet_jsonl),
        "record_count": len(records),
        "suggested_decision_counts": dict(decision_counts.most_common()),
        "token_delta_assessment_counts": dict(token_delta_counts.most_common()),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Use this aid to fill the original Markdown manually; then run extraction/diff preview read-only."
        ),
    }
    base = reports_dir() / f"{stamp()}_release_decision_post592_top15_decision_aid"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    md_lines = [
        "# Decision Aid - Post-592 Top 15",
        "",
        "Este arquivo Ã© auxiliar. As sugestÃµes abaixo NÃƒO sÃ£o decisÃ£o humana registrada.",
        "Copie/ajuste manualmente no pacote original apenas o que vocÃª aprovar.",
        "",
    ]
    for record in records:
        md_lines.extend(
            [
                f"## {record['packet_index']}. Segment {record['segment_id']}",
                f"- Source key: `{record['source_key']}`",
                f"- File/line: `{record['relative_path']}:{record['source_line_number']}`",
                f"- Open/high issues: `{record['open_issue_count']}` / `{record['high_issue_count']}`",
                f"- Primary risk: `{record['primary_risk']}`",
                f"- Token delta assessment: `{record['token_delta_assessment']}`",
                f"- Tokens/variables present: `{record['tokens_present']}`",
                f"- Assisted suggestion: `{record['suggested_human_decision']}`",
                f"- Rationale: {record['suggestion_rationale']}",
                "",
                "**English/source if available**",
                "",
                "```text",
                str(record.get("english_text") or ""),
                "```",
                "",
                "**Spanish/source**",
                "",
                "```text",
                str(record.get("spanish_text") or ""),
                "```",
                "",
                "**Current output**",
                "",
                "```text",
                str(record.get("output_text") or ""),
                "```",
                "",
                "**Current confirmed/target**",
                "",
                "```text",
                str(record.get("confirmed_text") or ""),
                "```",
                "",
                "**Suggested PT-BR corrected_text (assistant aid only)**",
                "",
                "```text",
                str(record.get("suggested_corrected_text") or ""),
                "```",
                "",
                "**Fields to copy/fill manually in original packet if approved**",
                "",
                "Human decision:",
                "",
                "Corrected text:",
                "",
                "Notes:",
                "",
            ]
        )
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"suggested_decision_counts={json.dumps(summary['suggested_decision_counts'], ensure_ascii=False)}")
    print(f"token_delta_assessment_counts={json.dumps(summary['token_delta_assessment_counts'], ensure_ascii=False)}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("materializer_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")
    print("source_changed=false")
    print("output_changed=false")


if __name__ == "__main__":
    main()
