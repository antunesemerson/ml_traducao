from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "narrative_dynamic_getter_gender_architecture_review_v1"
DEFAULT_INPUT = Path("reports/20260703_182038_026776_release_readiness_post544_diagnostic.jsonl")
DEFAULT_RUN_ID = 585
LIMIT = 120

TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$")
GETTER_RE = re.compile(r"\[[^\]]*(?:\.Get|\.Custom|ROOT\.|SCOPE\.|SelectLocalization|Select_CString)[^\]]+\]")
SELECT_RE = re.compile(r"Select_CString|SelectLocalization|AddLocalizationIf|LocalPlayerString", re.IGNORECASE)
SPANISH_LITERAL_RE = re.compile(
    r"\b(el|la|los|las|un|una|unos|unas|ese|esa|otra|otro|eres|está|estás|te|lo|la|los|las)\b|"
    r"guerrer[oa]|niñ[oa]|anfitri[oó]n|anfitriona|hereder[oa]|preocupad|nombrad",
    re.IGNORECASE,
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only narrative dynamic getter + gender architecture review.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--run-id", type=int, default=DEFAULT_RUN_ID)
    parser.add_argument("--limit", type=int, default=LIMIT)
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


def blob(row: dict[str, Any]) -> str:
    return "\n".join(str(row.get(key) or "") for key in ("source_text", "spanish_text", "output_text", "confirmed_text", "english_text", "source_key", "issue_kinds"))


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text or "")


def getter_tokens(row: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in ("output_text", "confirmed_text", "source_text", "spanish_text", "english_text"):
        found.extend(GETTER_RE.findall(str(row.get(key) or "")))
    deduped: list[str] = []
    for token in found:
        if token not in deduped:
            deduped.append(token)
    return deduped


def has_any(text: str, needles: list[str]) -> bool:
    low = text.lower()
    return any(needle.lower() in low for needle in needles)


def classify_roles(row: dict[str, Any], getter_list: list[str]) -> list[str]:
    text = blob(row)
    roles: list[str] = []
    if has_any(text, ["GetTitledFirstName", "GetShortUIName", "GetFullName", "GetFirstName", "GetName", "GetNameNoTooltip"]):
        roles.append("getter_person_name")
    if has_any(text, ["GetTitle", "GetPrimaryTitle", "GetLiege.GetDiarchTitle", "GetCouncilTitle", "GetCurrentLocation.GetName", "GetCapitalLocation", "GetDefender.GetName"]):
        roles.append("getter_title_or_realm")
    if has_any(text, ["GetFaith", "GetCulture", "Faith.Get", "Culture.Get", "GetAdherent", "GetReligion", "GetHeritage"]):
        roles.append("getter_faith/culture")
    if has_any(text, ["RelationToMe", "GetHerHis", "GetHisHer", "GetHerHim", "GetHerselfHimself", "GetSheHe", "Possessive"]):
        roles.append("getter_relation/possessive")
    if has_any(text, ["GetSheHe", "GetWomanMan", "Select_CString(actor.IsLocalPlayer", "SelectLocalization(IsLocalPlayer"]):
        roles.append("actor_pronoun_subject")
    if has_any(text, ["GetHerHim", "GetHerHis", "GetHisHer", "GetHerselfHimself", "Select_CString( CHARACTER.IsLocalPlayer, 'te'"]):
        roles.append("actor_pronoun_object")
    if has_any(text, ["ES_OA", "ES_XA", "ES_A", "ES_O", "traumatizad", "ocupado demais", "cert[", "nomead", "preocupad"]):
        roles.append("gendered_adjective")
    if has_any(text, ["ES_ElLa", "ES_LoLa", "ES_DelDela", "la guerrera", "el guerrero", "la anfitriona", "el anfitri", "hereder", "examinand", "niña", "niño"]):
        roles.append("gendered_noun")
    if SELECT_RE.search(text):
        roles.append("Select_CString/SelectLocalization overlap")
    if SPANISH_LITERAL_RE.search(text) and bool(row.get("spanish_residue_visible")):
        roles.append("literal Spanish residue near getter")
    if "\n" in str(row.get("output_text") or "") or "\\n" in str(row.get("output_text") or ""):
        roles.append("needs_parser_later")
    if int(row.get("high_issue_count") or 0) > 0 or "long_text" in str(row.get("issue_families") or ""):
        roles.append("human_context_required")
    if not roles and getter_list:
        roles.append("needs_parser_later")
    return roles


def primary_route(roles: list[str]) -> str:
    priority = [
        "Select_CString/SelectLocalization overlap",
        "getter_relation/possessive",
        "actor_pronoun_subject",
        "actor_pronoun_object",
        "gendered_adjective",
        "gendered_noun",
        "getter_faith/culture",
        "getter_title_or_realm",
        "getter_person_name",
        "literal Spanish residue near getter",
        "needs_parser_later",
        "human_context_required",
    ]
    for item in priority:
        if item in roles:
            return item
    return "needs_parser_later"


def parser_recommendation(roles: list[str], row: dict[str, Any]) -> str:
    if "Select_CString/SelectLocalization overlap" in roles:
        return "split_select_gender_perspective_parser_read_only"
    if "getter_relation/possessive" in roles or "actor_pronoun_object" in roles:
        return "relation_possessive_pronoun_parser_read_only"
    if "gendered_adjective" in roles or "gendered_noun" in roles:
        return "gender_agreement_parser_read_only"
    if "getter_faith/culture" in roles:
        return "faith_culture_getter_role_splitter_read_only"
    if "getter_title_or_realm" in roles:
        return "title_realm_getter_role_splitter_read_only"
    if "getter_person_name" in roles:
        return "person_name_getter_preserve_splitter_read_only"
    return "hold_parser_later_context"


def human_packet_potential(roles: list[str], row: dict[str, Any]) -> str:
    if int(row.get("high_issue_count") or 0) > 0:
        return "no_high_issue_parser_first"
    if "needs_parser_later" in roles or "Select_CString/SelectLocalization overlap" in roles:
        return "no_parser_first"
    if set(roles).issubset({"getter_person_name", "getter_title_or_realm"}):
        return "possible_small_human_packet_after_split"
    return "hold_until_parser_routes"


def qualify(row: dict[str, Any]) -> bool:
    if row.get("release_class") != "release_blocker":
        return False
    if row.get("visibility_group") != "narrative_events":
        return False
    if int(row.get("segment_id") or 0) in {120831, 127174}:
        return False
    text = blob(row)
    return (
        row.get("token_surface") == "dynamic_getter"
        or bool(row.get("gender_or_perspective"))
        or ".Get" in text
        or ".Custom" in text
    )


def record_from(row: dict[str, Any]) -> dict[str, Any]:
    getters = getter_tokens(row)
    roles = classify_roles(row, getters)
    route = primary_route(roles)
    return {
        "source": SOURCE,
        "record_type": "narrative_dynamic_getter_gender_architecture_review_item",
        "segment_id": int(row.get("segment_id") or 0),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "token_surface": row.get("token_surface"),
        "surface_flags": [
            *("dynamic_getter" for _ in [0] if row.get("token_surface") == "dynamic_getter"),
            *("gender_perspective" for _ in [0] if row.get("gender_or_perspective")),
        ],
        "architecture_roles": roles,
        "primary_route": route,
        "parser_recommendation": parser_recommendation(roles, row),
        "human_packet_potential": human_packet_potential(roles, row),
        "getter_tokens": getters[:12],
        "token_count": len(tokens(str(row.get("output_text") or row.get("confirmed_text") or ""))),
        "dominant_issue_family": row.get("dominant_issue_family") or "",
        "issue_families": row.get("issue_families") or "",
        "issue_kinds": row.get("issue_kinds") or "",
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "spanish_residue_visible": bool(row.get("spanish_residue_visible")),
        "gender_or_perspective": bool(row.get("gender_or_perspective")),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "source_text": row.get("spanish_text") or row.get("source_text"),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def balanced(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    per_key: Counter[str] = Counter()
    records = sorted(
        records,
        key=lambda r: (
            r["parser_recommendation"],
            r["primary_route"],
            -r["high_issue_count"],
            -r["open_issue_count"],
            r["relative_path"] or "",
            r["segment_id"],
        ),
    )
    for record in records:
        key = f"{record['parser_recommendation']}::{record['primary_route']}::{record['token_surface']}"
        if per_key[key] >= 10:
            continue
        selected.append(record)
        per_key[key] += 1
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        seen = {r["segment_id"] for r in selected}
        for record in records:
            if record["segment_id"] in seen:
                continue
            selected.append(record)
            seen.add(record["segment_id"])
            if len(selected) >= limit:
                break
    return selected


def markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Narrative Dynamic Getter + Gender Parser Review",
        "",
        f"- Segment-state run: {summary['segment_state_run_id']}",
        f"- Universo elegivel: {summary['eligible_count']}",
        f"- Amostra classificada: {summary['sample_count']}",
        "- Ações: read-only; sem candidato, apply, lifecycle, segment-state, reindex ou produção full.",
        "",
        "## Rotas Principais",
    ]
    for route, count in summary["primary_route_counts"].items():
        lines.append(f"- {route}: {count}")
    lines.extend(["", "## Recomendações De Parser/Subpolicy"])
    for rec, count in summary["parser_recommendation_counts"].items():
        lines.append(f"- {rec}: {count}")
    lines.extend(["", "## Potencial Humano"])
    for rec, count in summary["human_packet_potential_counts"].items():
        lines.append(f"- {rec}: {count}")
    lines.extend(["", "## Próxima Recomendação"])
    lines.append(summary["single_operational_recommendation"])
    lines.extend(["", "## Exemplos"])
    for row in rows[:40]:
        lines.append(
            f"- {row['segment_id']} | {row['primary_route']} | {row['parser_recommendation']} | "
            f"{row['source_key']} | tokens={', '.join(row['getter_tokens'][:3])}"
        )
    lines.append("")
    return "\n".join(lines)


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    all_rows = read_jsonl(args.input_jsonl)
    eligible = [record_from(row) for row in all_rows if qualify(row)]
    sample = balanced(eligible, args.limit)
    route_counts = Counter(row["primary_route"] for row in sample)
    role_counts = Counter(role for row in sample for role in row["architecture_roles"])
    parser_counts = Counter(row["parser_recommendation"] for row in sample)
    human_counts = Counter(row["human_packet_potential"] for row in sample)
    token_counts = Counter(row["token_surface"] for row in sample)
    getter_counter = Counter(token for row in sample for token in row["getter_tokens"])
    eligible_route_counts = Counter(row["primary_route"] for row in eligible)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_narrative_dynamic_getter_gender_architecture_review",
        "segment_state_run_id": args.run_id,
        "input_jsonl": str(args.input_jsonl),
        "scope": {
            "visibility_group": "narrative_events",
            "surfaces": ["dynamic_getter", "gender_perspective", "dynamic_getter+gender_perspective"],
            "excluded_segment_ids": [120831, 127174],
        },
        "eligible_count": len(eligible),
        "sample_count": len(sample),
        "eligible_primary_route_counts": dict(eligible_route_counts.most_common()),
        "primary_route_counts": dict(route_counts.most_common()),
        "architecture_role_counts": dict(role_counts.most_common()),
        "parser_recommendation_counts": dict(parser_counts.most_common()),
        "human_packet_potential_counts": dict(human_counts.most_common()),
        "token_surface_counts": dict(token_counts.most_common()),
        "top_getter_tokens": dict(getter_counter.most_common(25)),
        "split_only_policy_candidates": [
            {
                "policy": "person_name_getter_preserve_splitter_read_only",
                "count": parser_counts.get("person_name_getter_preserve_splitter_read_only", 0),
                "candidate_generation_allowed": False,
            },
            {
                "policy": "title_realm_getter_role_splitter_read_only",
                "count": parser_counts.get("title_realm_getter_role_splitter_read_only", 0),
                "candidate_generation_allowed": False,
            },
            {
                "policy": "gender_agreement_parser_read_only",
                "count": parser_counts.get("gender_agreement_parser_read_only", 0),
                "candidate_generation_allowed": False,
            },
        ],
        "safe_small_human_packet_potential_count": human_counts.get("possible_small_human_packet_after_split", 0),
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
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Register/design split-only read-only parser routes in this order: Select_CString/SelectLocalization gender-perspective overlap, "
            "relation/possessive pronoun parser, then gender agreement parser. Do not generate candidates yet; after architecture confirms routes, "
            "run dry-run routing on the full eligible set and only then consider a tiny human packet for person-name/title getter preservation cases."
        ),
    }
    return sample, summary, markdown(summary, sample)


def write(rows: list[dict[str, Any]], summary: dict[str, Any], md: str) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_narrative_dynamic_getter_gender_architecture_review"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    jsonl_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
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
    print(f"eligible_count={summary['eligible_count']}")
    print(f"sample_count={summary['sample_count']}")
    print(f"primary_route_counts={json.dumps(summary['primary_route_counts'], ensure_ascii=False)}")
    print(f"parser_recommendation_counts={json.dumps(summary['parser_recommendation_counts'], ensure_ascii=False)}")
    print(f"safe_small_human_packet_potential_count={summary['safe_small_human_packet_potential_count']}")
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
