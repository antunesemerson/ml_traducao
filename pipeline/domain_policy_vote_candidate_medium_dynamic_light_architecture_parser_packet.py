from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_architecture_parser_packet"
INPUT_JSONL = Path("reports/20260629_224912_810219_domain_policy_vote_candidate_medium_dynamic_light_remaining_holds_diagnostic.jsonl")
INPUT_SUMMARY = Path("reports/20260629_224912_810219_domain_policy_vote_candidate_medium_dynamic_light_remaining_holds_diagnostic_summary.json")

GROUP_MAP = {
    "getter_perspective_omitted": "getter_perspective_omitted",
    "pantheonterm_agreement": "PantheonTerm",
    "select_localization_select_cstring": "SelectLocalization/Select_CString",
    "generic_relation_or_possessive": "relation_or_possessive",
}

GROUP_RECOMMENDATIONS = {
    "getter_perspective_omitted": {
        "recommended_decision": "parser_proprio",
        "architecture_question": "Definir se getters de perspectiva omitidos no output sao erro obrigatorio de preservacao ou omissao semanticamente aceitavel por contexto.",
        "policy_recommendation": "Criar parser/policy read-only para detectar getters de perspectiva presentes no english/spanish e ausentes no output, sem gerar candidato automaticamente.",
        "candidate_generation_allowed": False,
        "human_packet_future": "Somente depois de a arquitetura classificar quais omissoes exigem reparo humano.",
    },
    "PantheonTerm": {
        "recommended_decision": "hold_explicito",
        "architecture_question": "Materializar regra de numero/concordancia para ROOT.Faith.PantheonTerm antes de qualquer correcao automatica.",
        "policy_recommendation": "Manter em hold ate existir policy que saiba se PantheonTerm expande como singular, plural, coletivo ou termo variavel por fe.",
        "candidate_generation_allowed": False,
        "human_packet_future": "Pode virar pacote humano pequeno para exemplos de singular/plural, mas nao antes de preservar a variabilidade do token.",
    },
    "SelectLocalization/Select_CString": {
        "recommended_decision": "parser_proprio",
        "architecture_question": "Separar SelectLocalization/Select_CString de medium_dynamic_light e tratar como superficie estrutural de selecao condicional.",
        "policy_recommendation": "Criar splitter/parser read-only para surface SelectLocalization/Select_CString, com validacao de afixos, pipe modifiers e alternativas internas.",
        "candidate_generation_allowed": False,
        "human_packet_future": "Apenas para casos em que o parser identifique afixo externo traduzivel sem alterar o token.",
    },
    "relation_or_possessive": {
        "recommended_decision": "subpolicy_read_only",
        "architecture_question": "Definir rota para Custom2('RelationToMe') e superficies de relacao/possessivo que exigem pessoa discursiva.",
        "policy_recommendation": "Absorver em policy read-only de relation/perspective getters; manter candidate generation bloqueada.",
        "candidate_generation_allowed": False,
        "human_packet_future": "Somente se aparecerem mais exemplos com mesmo padrao e contexto suficiente.",
    },
    "outros": {
        "recommended_decision": "hold_explicito",
        "architecture_question": "Classificar manualmente antes de qualquer roteamento.",
        "policy_recommendation": "Sem policy nova com volume baixo ou familia indefinida.",
        "candidate_generation_allowed": False,
        "human_packet_future": "Aguardar mais sinal.",
    },
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def classify_group(row: dict[str, Any]) -> str:
    return GROUP_MAP.get(str(row.get("hold_family") or ""), "outros")


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    group = classify_group(row)
    return {
        "segment_id": int(row["segment_id"]),
        "architecture_group": group,
        "hold_family": row.get("hold_family"),
        "surface_bucket": row.get("surface_bucket"),
        "route": row.get("route"),
        "subtype_review": row.get("subtype_review"),
        "role_tags": row.get("role_tags") or [],
        "dynamic_tokens": row.get("dynamic_tokens") or [],
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "current_output_text": row.get("current_output_text"),
        "existing_recommendation": row.get("recommendation"),
        "architecture_recommendation": GROUP_RECOMMENDATIONS[group],
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "requires_architecture_decision": True,
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_group[row["architecture_group"]].append(row)

    lines = [
        "medium_dynamic_light architecture parser packet",
        "",
        f"generated_at: {summary['generated_at']}",
        f"input_jsonl: {summary['input_jsonl']}",
        f"packet_count: {summary['packet_count']}",
        "",
        "guards:",
        "- candidate_generation: not_run",
        "- apply_output: not_run",
        "- lifecycle: not_run",
        "- segment_state: not_run",
        "- reindex: not_run",
        "- full_production: not_run",
        "",
        "group_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["group_counts"])
    lines.append("")
    lines.append("recommendations_by_group:")
    for group, recommendation in summary["recommendations_by_group"].items():
        lines.extend(
            [
                f"",
                f"## {group}",
                f"- decision: {recommendation['recommended_decision']}",
                f"- architecture_question: {recommendation['architecture_question']}",
                f"- policy_recommendation: {recommendation['policy_recommendation']}",
                f"- human_packet_future: {recommendation['human_packet_future']}",
            ]
        )
    lines.append("")
    lines.append("samples:")
    for group in sorted(by_group):
        lines.extend(["", f"## group: {group}"])
        for row in by_group[group]:
            lines.extend(
                [
                    "",
                    f"### segment_id {row['segment_id']} | {row['source_key']}",
                    f"- path: {row['relative_path']}",
                    f"- surface_bucket: {row['surface_bucket']}",
                    f"- hold_family: {row['hold_family']}",
                    f"- route: {row['route']}",
                    f"- dynamic_tokens: {json.dumps(row['dynamic_tokens'], ensure_ascii=False)}",
                    f"- english: {row['english_text']}",
                    f"- spanish: {row['spanish_text']}",
                    f"- output_ptbr: {row['current_output_text']}",
                    f"- objective_recommendation: {row['architecture_recommendation']['recommended_decision']}",
                ]
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    rows = load_jsonl(INPUT_JSONL)
    packet_rows = [normalize_row(row) for row in rows if row.get("operational_class") == "architecture_parser_later"]
    packet_rows.sort(key=lambda item: (item["architecture_group"], item["segment_id"]))

    group_counts = Counter(row["architecture_group"] for row in packet_rows)
    hold_family_counts = Counter(str(row["hold_family"]) for row in packet_rows)
    surface_counts = Counter(str(row["surface_bucket"]) for row in packet_rows)
    route_counts = Counter(str(row["route"]) for row in packet_rows)

    recommendations_by_group = {
        group: GROUP_RECOMMENDATIONS[group]
        for group in sorted(group_counts)
    }
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_architecture_parser_packet",
        "input_jsonl": str(INPUT_JSONL),
        "input_summary": str(INPUT_SUMMARY),
        "packet_count": len(packet_rows),
        "expected_packet_count": 10,
        "count_matches_expected": len(packet_rows) == 10,
        "group_counts": counter_rows(group_counts),
        "hold_family_counts": counter_rows(hold_family_counts),
        "surface_counts": counter_rows(surface_counts),
        "route_counts": counter_rows(route_counts),
        "recommendations_by_group": recommendations_by_group,
        "single_operational_recommendation": "Send these 10 to architecture as parser/policy design input only: create read-only parsers for getter perspective and SelectLocalization surfaces, keep PantheonTerm on explicit hold until number behavior is known, and route relation/possessive into a read-only relation/perspective policy.",
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_{SOURCE}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, packet_rows)
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, packet_rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
