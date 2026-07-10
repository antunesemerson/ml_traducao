from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_blocker_dynamic_parser_consolidation_readonly_v1"

DEFAULT_RELEASE_DYNAMIC_SUMMARY = Path("reports/20260703_183328_572135_release_blocker_dynamic_parser_diagnostic_summary.json")
DEFAULT_SPLITTER_SUMMARY = Path("reports/20260703_193709_147411_narrative_dynamic_getter_gender_splitter_readonly_summary.json")
DEFAULT_GENDER_METADATA_SUMMARY = Path("reports/20260703_191852_471406_narrative_gender_agreement_parser_metadata_readonly_summary.json")
DEFAULT_RUNTIME_SUMMARY = Path("reports/20260703_193847_633638_narrative_runtime_name_getter_preserve_review_readonly_summary.json")
DEFAULT_FAITH_CULTURE_SUMMARY = Path("reports/20260703_194220_492438_narrative_faith_culture_getter_role_review_readonly_summary.json")
DEFAULT_SELECT_REVIEW_SUMMARY = Path("reports/20260703_195859_357117_narrative_select_conditional_overlap_review_readonly_summary.json")
DEFAULT_SELECT_SUBPOLICY_SUMMARY = Path("reports/20260703_200250_925787_narrative_select_gender_perspective_subpolicy_readonly_summary.json")
DEFAULT_SELECT_PLAYER_METADATA_SUMMARY = Path("reports/20260703_201933_888089_narrative_select_player_perspective_metadata_readonly_summary.json")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only consolidation for release blocker dynamic/parser routes.")
    parser.add_argument("--release-dynamic-summary", type=Path, default=DEFAULT_RELEASE_DYNAMIC_SUMMARY)
    parser.add_argument("--splitter-summary", type=Path, default=DEFAULT_SPLITTER_SUMMARY)
    parser.add_argument("--gender-metadata-summary", type=Path, default=DEFAULT_GENDER_METADATA_SUMMARY)
    parser.add_argument("--runtime-summary", type=Path, default=DEFAULT_RUNTIME_SUMMARY)
    parser.add_argument("--faith-culture-summary", type=Path, default=DEFAULT_FAITH_CULTURE_SUMMARY)
    parser.add_argument("--select-review-summary", type=Path, default=DEFAULT_SELECT_REVIEW_SUMMARY)
    parser.add_argument("--select-subpolicy-summary", type=Path, default=DEFAULT_SELECT_SUBPOLICY_SUMMARY)
    parser.add_argument("--select-player-metadata-summary", type=Path, default=DEFAULT_SELECT_PLAYER_METADATA_SUMMARY)
    parser.add_argument("--run-id", type=int, default=585)
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    resolved = db.project_path(path)
    with resolved.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def artifact_record(label: str, path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact": label,
        "path": str(path),
        "source": summary.get("source"),
        "mode": summary.get("mode"),
        "candidate_generation_count": summary.get("candidate_generation_count", 0),
        "apply_count": summary.get("apply_count", 0),
        "learning_ingest_count": summary.get("learning_ingest_count", 0),
        "issue_closure_count": summary.get("issue_closure_count", 0),
        "lifecycle_count": summary.get("lifecycle_count", 0),
        "materializer_count": summary.get("materializer_count", 0),
        "segment_state_count": summary.get("segment_state_count", 0),
        "reindex_count": summary.get("reindex_count", 0),
        "production_full_count": summary.get("production_full_count", 0),
    }


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    artifacts = {
        "release_blocker_dynamic_parser_diagnostic": (args.release_dynamic_summary, load_json(args.release_dynamic_summary)),
        "narrative_dynamic_getter_gender_splitter_readonly": (args.splitter_summary, load_json(args.splitter_summary)),
        "narrative_gender_agreement_parser_metadata_readonly": (
            args.gender_metadata_summary,
            load_json(args.gender_metadata_summary),
        ),
        "narrative_runtime_name_getter_preserve_review_readonly": (args.runtime_summary, load_json(args.runtime_summary)),
        "narrative_faith_culture_getter_role_review_readonly": (
            args.faith_culture_summary,
            load_json(args.faith_culture_summary),
        ),
        "narrative_select_conditional_overlap_review_readonly": (
            args.select_review_summary,
            load_json(args.select_review_summary),
        ),
        "narrative_select_gender_perspective_subpolicy_readonly": (
            args.select_subpolicy_summary,
            load_json(args.select_subpolicy_summary),
        ),
        "narrative_select_player_perspective_metadata_readonly": (
            args.select_player_metadata_summary,
            load_json(args.select_player_metadata_summary),
        ),
    }

    splitter = artifacts["narrative_dynamic_getter_gender_splitter_readonly"][1]
    gender = artifacts["narrative_gender_agreement_parser_metadata_readonly"][1]
    runtime = artifacts["narrative_runtime_name_getter_preserve_review_readonly"][1]
    faith_culture = artifacts["narrative_faith_culture_getter_role_review_readonly"][1]
    select_review = artifacts["narrative_select_conditional_overlap_review_readonly"][1]
    select_subpolicy = artifacts["narrative_select_gender_perspective_subpolicy_readonly"][1]
    select_player = artifacts["narrative_select_player_perspective_metadata_readonly"][1]
    release_dynamic = artifacts["release_blocker_dynamic_parser_diagnostic"][1]

    final_routes = [
        {
            "final_route": "parser_later_gender_agreement_dependency",
            "volume": gender.get("record_count", 0),
            "source_artifact": "narrative_gender_agreement_parser_metadata_readonly",
            "basis": f"parser_metadata_ok={gender.get('risk_counts', {}).get('parser_metadata_ok', 0)}; risks={gender.get('risk_counts', {})}",
            "human_packet_now": False,
            "future_gap": "Parser de concordancia por dependencia de genero/ator/recipiente e contexto lexical.",
        },
        {
            "final_route": "parser_later_runtime_name_getter_context",
            "volume": runtime.get("record_count", 0),
            "source_artifact": "narrative_runtime_name_getter_preserve_review_readonly",
            "basis": f"review_decisions={runtime.get('review_decision_counts', {})}",
            "human_packet_now": False,
            "future_gap": "Parser preserve-only para runtime names, separando activity/artifact/title/character depois de reduzir high issue context.",
        },
        {
            "final_route": "parser_later_faith_culture_getter_context",
            "volume": faith_culture.get("record_count", 0),
            "source_artifact": "narrative_faith_culture_getter_role_review_readonly",
            "basis": f"classes={faith_culture.get('faith_culture_class_counts', {})}; decisions={faith_culture.get('review_decision_counts', {})}",
            "human_packet_now": False,
            "future_gap": "Subpolicy faith/culture getter em shadow apenas para classes puras; overlaps ficam em hold.",
        },
        {
            "final_route": "parser_later_select_cstring_local_player_perspective",
            "volume": select_player.get("record_count", 0),
            "source_artifact": "narrative_select_player_perspective_metadata_readonly",
            "basis": f"metadata_only_ok={select_player.get('risk_counts', {}).get('metadata_only_ok', 0)}; dependencies={select_player.get('dependency_counts', {})}",
            "human_packet_now": False,
            "future_gap": "Parser de perspectiva local/player para CHARACTER/TARGET_CHARACTER/ROOT.Char IsLocalPlayer.",
        },
        {
            "final_route": "parser_later_multiline_select",
            "volume": select_review.get("review_decision_counts", {}).get("parser_later_multiline_select", 0),
            "source_artifact": "narrative_select_conditional_overlap_review_readonly",
            "basis": "multiline Select_CString/SelectLocalization deve ir para parser-later terminal.",
            "human_packet_now": False,
            "future_gap": "Parser multiline/select com contexto narrativo; nao adequado para pacote manual pequeno.",
        },
        {
            "final_route": "hold_relation_possessive",
            "volume": splitter.get("route_counts", {}).get("hold_getter_relation_possessive", 0),
            "source_artifact": "narrative_dynamic_getter_gender_splitter_readonly",
            "basis": "relation/possessive getter depende de papel sintatico e perspectiva.",
            "human_packet_now": False,
            "future_gap": "Parser de relacao/possessivo antes de qualquer correcao ou pacote humano.",
        },
        {
            "final_route": "hold_literal_spanish_near_getter",
            "volume": splitter.get("route_counts", {}).get("hold_literal_spanish_near_getter", 0),
            "source_artifact": "narrative_dynamic_getter_gender_splitter_readonly",
            "basis": "residuo espanhol perto de getter dinamico exige contexto semantico.",
            "human_packet_now": False,
            "future_gap": "Triagem semantica/parser especifico para espanhol literal adjacente a getter.",
        },
    ]

    no_human_now = [
        {
            "group": row["final_route"],
            "volume": row["volume"],
            "reason": row["basis"],
        }
        for row in final_routes
    ]

    route_volume_total = sum(int(row["volume"] or 0) for row in final_routes)
    operations = [artifact_record(label, path, summary) for label, (path, summary) in artifacts.items()]
    operation_totals = Counter()
    for record in operations:
        for key in (
            "candidate_generation_count",
            "apply_count",
            "learning_ingest_count",
            "issue_closure_count",
            "lifecycle_count",
            "materializer_count",
            "segment_state_count",
            "reindex_count",
            "production_full_count",
        ):
            operation_totals[key] += int(record.get(key) or 0)

    next_front = {
        "recommendation": "Migrar para frente de release com fechamento real fora de dynamic/parser: triagem high_issue nao dinamica ou pacote plain/light revisavel.",
        "avoid_now": [
            "narrative_events dynamic_getter/dynamic_select",
            "Select_CString local-player/perspective",
            "gender agreement dependency",
            "runtime-name getter context",
            "relation/possessive getter",
            "literal Spanish near getter",
        ],
        "candidate_next_probe": (
            "Rodar sondagem read-only de release_blocker nao dinamico: plain_text/light_token, sem getter/select/multiline/parser_later, "
            "excluindo high_issue_auditor quando a meta for fechamento rapido; se o volume for baixo, abrir triagem high_issue especifica."
        ),
        "why": (
            "Esta familia dynamic/parser consolidou volume alto, mas nao produziu metadata_only_ok suficiente nem sublote humano seguro. "
            "Continuar aqui agora tende a gerar parser design, nao fechamento."
        ),
    }

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_release_blocker_dynamic_parser_consolidation",
        "segment_state_run_id": args.run_id,
        "included_artifacts": {label: str(path) for label, (path, _summary) in artifacts.items()},
        "release_blocker_count": release_dynamic.get("release_blocker_count"),
        "release_blocker_dynamic_parser_ratio_pct": release_dynamic.get("release_blocker_dynamic_parser_ratio_pct"),
        "target_group_counts": release_dynamic.get("target_group_counts", {}),
        "surface_flag_counts": release_dynamic.get("surface_flag_counts", {}),
        "final_route_counts": {row["final_route"]: row["volume"] for row in final_routes},
        "final_route_volume_total": route_volume_total,
        "do_not_send_to_human_packet_now": no_human_now,
        "architecture_gaps": {row["final_route"]: row["future_gap"] for row in final_routes},
        "next_release_front_recommendation": next_front,
        "operation_totals": dict(operation_totals),
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
        "production_full_recommended_now": False,
        "single_operational_recommendation": next_front["recommendation"],
    }
    return final_routes + operations, summary, markdown(summary, final_routes)


def markdown(summary: dict[str, Any], final_routes: list[dict[str, Any]]) -> str:
    lines = [
        "# Release Blocker Dynamic/Parser Consolidation",
        "",
        f"- Segment-state run base: {summary['segment_state_run_id']}",
        f"- Release blockers no diagnostico base: {summary['release_blocker_count']}",
        f"- Dynamic/parser ratio: {summary['release_blocker_dynamic_parser_ratio_pct']}%",
        "- Acoes: read-only; sem apply, ingest, issue closure, lifecycle/materializer, segment-state, reindex ou producao full.",
        "",
        "## Rotas Finais",
    ]
    for row in final_routes:
        lines.append(f"- {row['final_route']}: {row['volume']} | {row['basis']}")
    lines.extend(["", "## Nao Enviar Para Pacote Humano Agora"])
    for row in final_routes:
        lines.append(f"- {row['final_route']} ({row['volume']}): {row['future_gap']}")
    lines.extend(["", "## Lacunas Arquiteturais"])
    for route, gap in summary["architecture_gaps"].items():
        lines.append(f"- {route}: {gap}")
    lines.extend(["", "## Proxima Frente Recomendada"])
    lines.append(summary["next_release_front_recommendation"]["recommendation"])
    lines.append("")
    lines.append(summary["next_release_front_recommendation"]["candidate_next_probe"])
    lines.extend(["", "## Guards"])
    for key, value in summary["operation_totals"].items():
        lines.append(f"- {key}: {value}")
    lines.append("")
    return "\n".join(lines)


def write(rows: list[dict[str, Any]], summary: dict[str, Any], md: str) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_release_blocker_dynamic_parser_consolidation_readonly"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    jsonl_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    md_path.write_text(md, encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return summary["output_files"]


def main() -> None:
    args = parse_args()
    rows, summary, md = build(args)
    outputs = write(rows, summary, md)
    print(f"markdown={outputs['markdown']}")
    print(f"jsonl={outputs['jsonl']}")
    print(f"summary={outputs['summary']}")
    print(f"final_route_counts={json.dumps(summary['final_route_counts'], ensure_ascii=False)}")
    print(f"final_route_volume_total={summary['final_route_volume_total']}")
    print(f"operation_totals={json.dumps(summary['operation_totals'], ensure_ascii=False)}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("materializer_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
