from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "narrative_gender_agreement_parser_metadata_readonly_v1"
DEFAULT_INPUT = Path("reports/20260703_190214_172429_narrative_dynamic_getter_gender_splitter_readonly.jsonl")
DEFAULT_RUN_ID = 585
TARGET_ROUTE = "route_gender_agreement_parser_read_only"

TOKEN_RE = re.compile(r"\[[^\]]+\]")
GENDER_TOKEN_RE = re.compile(
    r"\[[^\]]*(?:ES_OA|ES_XA|ES_A|ES_O|ES_ElLa|ES_LoLa|ES_AlAla|ES_DelDela|GetWomanMan|GetLadyLord|GetDaughterSon)[^\]]*\]"
)
PT_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ#_']+")
SPANISH_RE = re.compile(
    r"\b(mucho|ganando|aplicarlo|el|la|los|las|un|una|est[aá]|eres|nombrad[oa]?|preocupad[oa]?|encantad[oa]?)\b",
    re.IGNORECASE,
)
SELECT_RE = re.compile(r"Select_CString|SelectLocalization|AddLocalizationIf|LocalPlayerString", re.IGNORECASE)
RELATION_RE = re.compile(r"GetHerHis|GetHisHer|GetHerHim|GetHerselfHimself|GetSheHe|RelationToMe|Possessive")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only parser metadata for narrative gender agreement route.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--run-id", type=int, default=DEFAULT_RUN_ID)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    resolved = db.project_path(path)
    rows: list[dict[str, Any]] = []
    with resolved.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def text_blob(row: dict[str, Any]) -> str:
    return "\n".join(
        str(row.get(key) or "")
        for key in ("source_text", "output_text", "confirmed_text", "source_key", "issue_kinds", "issue_families")
    )


def choose_main_token(row: dict[str, Any]) -> str:
    text = str(row.get("output_text") or row.get("confirmed_text") or row.get("source_text") or "")
    gender_match = GENDER_TOKEN_RE.search(text)
    if gender_match:
        return gender_match.group(0)
    for token in row.get("getter_tokens") or []:
        if any(marker in token for marker in ("ES_", "GetWomanMan", "GetLadyLord", "GetDaughterSon")):
            return token
    getters = row.get("getter_tokens") or []
    return getters[0] if getters else ""


def context_around(text: str, token: str, width: int = 64) -> str:
    if not text or not token:
        return ""
    idx = text.find(token)
    if idx < 0:
        return text[: width * 2].replace("\n", "\\n")
    start = max(0, idx - width)
    end = min(len(text), idx + len(token) + width)
    return text[start:end].replace("\n", "\\n")


def candidate_term(text: str, token: str) -> str:
    if not text or not token:
        return ""
    idx = text.find(token)
    if idx < 0:
        return ""
    before = text[max(0, idx - 40) : idx]
    after = text[idx + len(token) : idx + len(token) + 40]
    before_words = PT_WORD_RE.findall(before)
    after_words = PT_WORD_RE.findall(after)
    if before_words:
        term = before_words[-1].strip("_#'")
        if len(term) >= 2:
            return term
    if after_words:
        return after_words[0].strip("_#'")
    return ""


def agreement_class(row: dict[str, Any], token: str, term: str) -> str:
    roles = set(row.get("architecture_roles") or [])
    blob = text_blob(row)
    low_term = term.lower()
    if "gendered_noun" in roles or any(marker in token for marker in ("ES_ElLa", "ES_LoLa", "ES_AlAla", "GetWomanMan", "GetLadyLord", "GetDaughterSon")):
        return "gendered_noun"
    if "gendered_adjective" in roles and (
        low_term.endswith(("ad", "ado", "ada", "ido", "ida", "oso", "osa", "ento", "enta", "ulo", "ula"))
        or any(fragment in blob.lower() for fragment in ("encantad[", "incrédul[", "pequen[", "preocupad[", "nomead["))
    ):
        return "participle/adjectival_phrase"
    if "gendered_adjective" in roles:
        return "gendered_adjective"
    return "uncertain_agreement"


def likely_target(row: dict[str, Any], token: str) -> str:
    blob = text_blob(row)
    if any(marker in blob for marker in ("GetFaith", "GetCulture", "Faith.", "Culture.", "GetReligion", "GetHeritage")):
        return "faith/culture"
    if any(marker in blob for marker in ("GetTitle", "GetPrimaryTitle", "GetTitledFirstName", "GetCurrentLocation", "GetCapitalLocation")):
        return "title/realm"
    if any(marker in blob for marker in ("GetActivityType", "GetScheme", "GetAccoladeType", "GetTrait", "SCOPE.Custom('BG_GameType')")):
        return "artifact/activity/runtime_name"
    if any(marker in blob for marker in ("GetFirstName", "GetShortUIName", "GetFullName", "GetName", "ROOT.Char", "CHARACTER", "actor", "recipient")):
        return "character/person"
    return "unknown"


def dependency(row: dict[str, Any], token: str) -> str:
    blob = text_blob(row)
    if "IsLocalPlayer" in blob or "GetPlayer" in blob:
        return "local_player_perspective"
    if any(marker in blob for marker in ("recipient.", "target.", "victim.", "defender.", "attacker.")):
        return "gender_of_recipient"
    if any(marker in token for marker in ("ES_ElLa", "ES_LoLa", "ES_AlAla", "ES_DelDela")):
        return "static_grammatical_gender"
    if any(marker in token for marker in ("ES_OA", "ES_XA", "ES_A", "ES_O", "GetWomanMan", "GetLadyLord", "GetDaughterSon")):
        return "gender_of_actor"
    return "unknown"


def risk(row: dict[str, Any]) -> str:
    blob = text_blob(row)
    if SELECT_RE.search(blob):
        return "select_overlap"
    if RELATION_RE.search(blob):
        return "relation_or_possessive_overlap"
    if row.get("spanish_residue_visible") or SPANISH_RE.search(str(row.get("output_text") or "")):
        return "literal_spanish_overlap"
    if "needs_parser_later" in set(row.get("architecture_roles") or []) or int(row.get("high_issue_count") or 0) > 0:
        return "needs_context"
    return "parser_metadata_ok"


def metadata_record(row: dict[str, Any]) -> dict[str, Any]:
    text = str(row.get("output_text") or row.get("confirmed_text") or row.get("source_text") or "")
    token = choose_main_token(row)
    term = candidate_term(text, token)
    return {
        "source": SOURCE,
        "record_type": "narrative_gender_agreement_parser_metadata_item",
        "segment_id": int(row.get("segment_id") or 0),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "main_getter_or_token": token,
        "context_around_getter": context_around(text, token),
        "pt_br_candidate_agreement_term": term,
        "agreement_class": agreement_class(row, token, term),
        "likely_target": likely_target(row, token),
        "dependency": dependency(row, token),
        "risk": risk(row),
        "architecture_roles": row.get("architecture_roles") or [],
        "getter_tokens": row.get("getter_tokens") or [],
        "token_surface": row.get("token_surface"),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "issue_families": row.get("issue_families") or "",
        "issue_kinds": row.get("issue_kinds") or "",
        "source_text": row.get("source_text"),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def top_tokens_by_risk(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        token = row.get("main_getter_or_token") or "none"
        counters[row["risk"]][token] += 1
    return {risk_name: dict(counter.most_common(12)) for risk_name, counter in sorted(counters.items())}


def examples(rows: list[dict[str, Any]], field: str, limit: int = 6) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: (r[field], -r["high_issue_count"], r["segment_id"])):
        key = row[field]
        if len(grouped[key]) >= limit:
            continue
        grouped[key].append(
            {
                "segment_id": row["segment_id"],
                "source_key": row["source_key"],
                "main_getter_or_token": row["main_getter_or_token"],
                "term": row["pt_br_candidate_agreement_term"],
                "context": row["context_around_getter"],
                "risk": row["risk"],
            }
        )
    return dict(grouped)


def recommendation(summary: dict[str, Any]) -> str:
    ok = summary["risk_counts"].get("parser_metadata_ok", 0)
    needs = summary["risk_counts"].get("needs_context", 0)
    if ok >= 25 and ok > needs:
        return "Existe sublote potencial para human review depois de parser metadata, mas ainda sem candidate generation."
    return (
        "Tratar como parser-later/metadata primeiro. A rota ainda tem contexto alto demais para pacote humano seguro "
        "ou regra automática; usar os metadados para desenhar parser de concordancia antes de nova revisao manual."
    )


def markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Narrative Gender Agreement Parser Metadata Read-Only",
        "",
        f"- Segment-state run base: {summary['segment_state_run_id']}",
        f"- Entrada: `{summary['input_jsonl']}`",
        f"- Registros da rota: {summary['record_count']}",
        "- Acoes: read-only; sem candidato, apply, ingest, issue closure, lifecycle/materializer, segment-state, reindex ou producao full.",
        "",
        "## Classe",
    ]
    for key, count in summary["agreement_class_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Alvo Provavel"])
    for key, count in summary["likely_target_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Dependencia"])
    for key, count in summary["dependency_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Risco"])
    for key, count in summary["risk_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Recomendacao"])
    lines.append(summary["single_operational_recommendation"])
    lines.extend(["", "## Nota Para Ajuste Do Splitter"])
    lines.append(summary["splitter_rename_recommendation"])
    lines.extend(["", "## Exemplos Por Classe"])
    for key, items in summary["examples_by_class"].items():
        lines.append(f"### {key}")
        for item in items[:4]:
            lines.append(
                f"- {item['segment_id']} | {item['source_key']} | {item['main_getter_or_token']} | "
                f"termo={item['term']} | risco={item['risk']}"
            )
    lines.append("")
    return "\n".join(lines)


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    input_rows = [row for row in read_jsonl(args.input_jsonl) if row.get("route") == TARGET_ROUTE]
    rows = [metadata_record(row) for row in input_rows]
    class_counts = Counter(row["agreement_class"] for row in rows)
    target_counts = Counter(row["likely_target"] for row in rows)
    dependency_counts = Counter(row["dependency"] for row in rows)
    risk_counts = Counter(row["risk"] for row in rows)
    token_counts = Counter(row["main_getter_or_token"] or "none" for row in rows)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_parser_metadata",
        "segment_state_run_id": args.run_id,
        "input_jsonl": str(args.input_jsonl),
        "scope": {
            "route": TARGET_ROUTE,
            "expected_count": 212,
            "candidate_generation_allowed": False,
            "apply_allowed": False,
            "learning_ingest_allowed": False,
            "issue_closure_allowed": False,
            "lifecycle_or_materializer_allowed": False,
            "segment_state_allowed": False,
            "reindex_allowed": False,
            "production_full_allowed": False,
        },
        "record_count": len(rows),
        "expected_count_ok": len(rows) == 212,
        "agreement_class_counts": dict(class_counts.most_common()),
        "likely_target_counts": dict(target_counts.most_common()),
        "dependency_counts": dict(dependency_counts.most_common()),
        "risk_counts": dict(risk_counts.most_common()),
        "top_main_getter_or_token": dict(token_counts.most_common(25)),
        "top_tokens_by_risk": top_tokens_by_risk(rows),
        "examples_by_class": examples(rows, "agreement_class"),
        "examples_by_risk": examples(rows, "risk"),
        "safe_human_review_sublote_now": False,
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
        "splitter_rename_recommendation": (
            "Renomear route_person_name_getter_preserve_splitter_read_only para "
            "route_runtime_name_getter_preserve_splitter_read_only em proximo ajuste, "
            "pois a rota tambem cobre nomes runtime de atividade/esquema/accolade."
        ),
    }
    summary["single_operational_recommendation"] = recommendation(summary)
    return rows, summary, markdown(summary, rows)


def write(rows: list[dict[str, Any]], summary: dict[str, Any], md: str) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_narrative_gender_agreement_parser_metadata_readonly"
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
    print(f"record_count={summary['record_count']}")
    print(f"expected_count_ok={summary['expected_count_ok']}")
    print(f"agreement_class_counts={json.dumps(summary['agreement_class_counts'], ensure_ascii=False)}")
    print(f"likely_target_counts={json.dumps(summary['likely_target_counts'], ensure_ascii=False)}")
    print(f"dependency_counts={json.dumps(summary['dependency_counts'], ensure_ascii=False)}")
    print(f"risk_counts={json.dumps(summary['risk_counts'], ensure_ascii=False)}")
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
