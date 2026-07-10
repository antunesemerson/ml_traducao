from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import domain_policy_vote_candidate_deep_diagnostic as deep_diagnostic
import domain_policy_vote_candidate_human_packet as human_packet


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_diagnostic_v1"
DEFAULT_SEGMENT_STATE_RUN_ID = 495
TARGET_RISK = "medium_dynamic_light"
PRIORITY_SUBLANES = ("religion_faith_doctrine", "title_realm_governance")
SPECIAL_GETTER_OMISSION_SEGMENT_ID = 109834
SAMPLE_PER_CLASS = 8

GETTER_RE = re.compile(r"\[[^\]]*Get[A-Za-z0-9_]+[^\]]*\]|\b(?:ROOT|FROM|SCOPE|TARGET)\.|Get[A-Za-z0-9_]+")
PERSPECTIVE_RE = re.compile(r"\b(?:GetHerHim|GetSheHe|GetWomanMan|GetGirlBoy|GetHerselfHimself|GetHerHis|GetHerHisMy|GetHerHisTheir)\b")
ARTICLE_GENDER_RE = re.compile(r"\b(?:[oa]/[ao]s?|um/uma|o/a|do/da|dos/das|este/esta|esse/essa|aquele/aquela|dele/dela)\b|#EMP|\.Custom\('ES_", re.IGNORECASE)
STYLE_RE = re.compile(
    r"\b(?:atrav[eé]s|realmente|atual|fazer|fizeram|fazem|coisa|tomando o controle|muito|pr[oó]prio|"
    r"dom[ií]nio|senhorio|suserano|impiedosamente|jogo|mal-apropriado|recebe cr[eé]dito)\b",
    re.IGNORECASE,
)
DOMAIN_HEAVY_RE = re.compile(
    r"\b(?:faith|religion|doctrine|tenet|holy|piety|title|realm|liege|vassal|kingdom|culture|tradition|innovation)\b",
    re.IGNORECASE,
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_paths() -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_medium_dynamic_light_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir() / f"{base.name}_summary.json"


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def short(text: str | None, limit: int = 700) -> str:
    value = str(text or "")
    compact = value.replace("\r\n", "\\n").replace("\n", "\\n")
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def text_blob(row: dict[str, Any]) -> str:
    return "\n".join(
        str(row.get(key) or "")
        for key in ("english_text", "spanish_text", "current_output_text", "relative_path", "source_key")
    )


def output_token(row: dict[str, Any]) -> str:
    current = str(row.get("current_output_text") or "")
    tokens = deep_diagnostic.TOKEN_RE.findall(current)
    return tokens[0] if tokens else ""


def classify(row: dict[str, Any], learned_or_hold: set[int], high_severity_ids: set[int]) -> tuple[str, str, list[str]]:
    segment_id = int(row["segment_id"])
    current = str(row.get("current_output_text") or "")
    english = str(row.get("english_text") or "")
    spanish = str(row.get("spanish_text") or "")
    blob = text_blob(row)
    token = output_token(row)
    reasons: list[str] = []

    if segment_id in learned_or_hold:
        return "hold_contextual", "already learned/held/rejected/corrected locally", ["already_learned_or_hold"]
    if segment_id in high_severity_ids:
        return "hold_contextual", "open high-severity issue", ["open_high_severity_issue"]
    if segment_id in human_packet.KNOWN_STRUCTURAL_BLOCKED_SEGMENT_IDS:
        return "hold_contextual", "known structural block", ["known_structural_blocked"]

    source_getter = bool(GETTER_RE.search(english) or GETTER_RE.search(spanish))
    output_getter = bool(GETTER_RE.search(current))
    perspective_getter = bool(PERSPECTIVE_RE.search(blob))
    if source_getter and not output_getter:
        reasons.append("source_getter_not_preserved_in_output")
        return "getter_perspective_omitted", "source contains getter/perspective surface that output does not preserve", reasons
    if perspective_getter:
        reasons.append("perspective_getter_surface")
        return "getter_perspective_omitted", "perspective getter requires architecture/perspective policy", reasons
    if GETTER_RE.search(token):
        reasons.append("getter_token")
        return "precisa_arquitetura", "single token is a getter/scope expression; needs parser/perspective policy", reasons
    if ARTICLE_GENDER_RE.search(blob):
        reasons.append("article_gender_surface")
        return "artigo_genero_token_leve", "light token with article/gender agreement surface", reasons
    if STYLE_RE.search(current):
        reasons.append("style_or_literal_surface")
        return "estilo_fluencia_token_leve", "light token with style/fluency or literalness signal", reasons
    if DOMAIN_HEAVY_RE.search(english) or DOMAIN_HEAVY_RE.search(spanish):
        reasons.append("domain_semantic_surface")
        return "precisa_humano", "domain semantics need human/domain review despite single token", reasons
    if token and not any(marker in token for marker in ("Select_CString", "Get", ".Custom('ES_")):
        reasons.append("single_non_getter_token")
        return "token_simples_preservavel", "single non-getter token appears preservable; suitable for later narrow human packet/dry-run", reasons
    return "precisa_humano", "unclassified single-token case needs human review", ["unclassified_medium_dynamic_light"]


def special_getter_omission(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            o.portuguese_text AS current_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id = ?
        """,
        (SPECIAL_GETTER_OMISSION_SEGMENT_ID,),
    ).fetchone()
    if not row:
        return None
    record = dict(row)
    return {
        "segment_id": int(record["segment_id"]),
        "relative_path": record.get("relative_path"),
        "source_key": record.get("source_key"),
        "source_line_number": record.get("source_line_number"),
        "english_text": record.get("english_text"),
        "spanish_text": record.get("spanish_text"),
        "current_output_text": record.get("current_output_text"),
        "classification": "getter_perspective_omitted",
        "note": "explicitly tracked outside low_plain_domain common flow because source has [councillor_liege.GetHerHim] and output omits the getter surface",
    }


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def representative_samples(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(str(record["surface_bucket"]), str(record["operational_class"]))].append(record)
    sample: list[dict[str, Any]] = []
    for key in sorted(grouped, key=lambda item: (item[0], item[1])):
        sample.extend(grouped[key][:SAMPLE_PER_CLASS])
    return sample


def build_summary(
    records: list[dict[str, Any]],
    special: dict[str, Any] | None,
    ledger_run_id: int,
    preflight_path: Path | None,
    preflight_count: int,
    segment_state_run_id: int,
) -> dict[str, Any]:
    surface_counts = Counter(str(row["surface_bucket"]) for row in records)
    class_counts = Counter(str(row["operational_class"]) for row in records)
    surface_class_counts = Counter(f"{row['surface_bucket']} | {row['operational_class']}" for row in records)
    priority_records = [row for row in records if row["surface_bucket"] in PRIORITY_SUBLANES]
    priority_class_counts = Counter(f"{row['surface_bucket']} | {row['operational_class']}" for row in priority_records)
    recommendation = (
        "review token_simples_preservavel and estilo_fluencia_token_leve samples first; send getter_perspective_omitted/precisa_arquitetura to architecture if recurring"
    )
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": segment_state_run_id,
        "ledger_run_id": ledger_run_id,
        "lane": "domain_policy_vote_candidate",
        "risk_bucket": TARGET_RISK,
        "preflight_summary_path": str(preflight_path) if preflight_path else None,
        "preflight_excluded_segment_count": preflight_count,
        "record_count": len(records),
        "priority_sublanes": list(PRIORITY_SUBLANES),
        "priority_record_count": len(priority_records),
        "surface_bucket_counts": top_counter(surface_counts),
        "operational_class_counts": top_counter(class_counts),
        "surface_operational_class_counts": top_counter(surface_class_counts),
        "priority_surface_operational_class_counts": top_counter(priority_class_counts),
        "special_getter_omission_outside_scope": special,
        "representative_sample_count": len(representative_samples(records)),
        "recommended_next_step": recommendation,
        "recommended_next_prompt": "medium_dynamic_light_operational_review_or_architecture_split",
        "apply_ready_now": False,
        "lifecycle_ready_now": False,
        "production_full_recommended_now": False,
        "ran_apply": False,
        "ran_lifecycle": False,
        "ran_segment_state": False,
        "ran_reindex": False,
        "ran_production_full": False,
        "source_changed": False,
        "output_changed": False,
    }


def write_outputs(summary: dict[str, Any], records: list[dict[str, Any]], sample: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light diagnostic",
        f"source={SOURCE}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        f"record_count={summary['record_count']}",
        f"priority_record_count={summary['priority_record_count']}",
        "",
        "surface_bucket_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["surface_bucket_counts"])
    lines.extend(["", "operational_class_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["operational_class_counts"])
    lines.extend(["", "priority_surface_operational_class_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["priority_surface_operational_class_counts"])
    if summary["special_getter_omission_outside_scope"]:
        special = summary["special_getter_omission_outside_scope"]
        lines.extend(
            [
                "",
                "special_getter_omission_outside_scope:",
                f"- segment_id={special['segment_id']} key={special['source_key']}",
                f"- note={special['note']}",
            ]
        )
    lines.extend(["", "representative_samples:"])
    for row in sample:
        lines.extend(
            [
                "",
                f"## {row['segment_id']} | {row['surface_bucket']} | {row['operational_class']}",
                f"- source_key: {row.get('source_key')}",
                f"- token: {row.get('output_token')}",
                f"- classification_reason: {row.get('classification_reason')}",
                f"- english_text: {row.get('english_text')}",
                f"- spanish_text: {row.get('spanish_text')}",
                f"- current_output_text: {row.get('current_output_text')}",
            ]
        )
    lines.extend(
        [
            "",
            f"recommended_next_step={summary['recommended_next_step']}",
            f"recommended_next_prompt={summary['recommended_next_prompt']}",
            "apply_ready_now=false",
            "lifecycle_ready_now=false",
            "production_full_recommended_now=false",
            "ran_apply=false",
            "ran_lifecycle=false",
            "ran_segment_state=false",
            "ran_reindex=false",
            "ran_production_full=false",
            "source_changed=false",
            "output_changed=false",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, default=DEFAULT_SEGMENT_STATE_RUN_ID)
    args = parser.parse_args()
    segment_state_run_id = int(args.segment_state_run_id)

    preflight_path, excluded_segment_ids = deep_diagnostic.load_preflight_exclusions()
    with connect_readonly() as conn:
        ledger_run_id = deep_diagnostic.latest_ledger_run_id(conn)
        rows = deep_diagnostic.fetch_rows(conn, segment_state_run_id, ledger_run_id, excluded_segment_ids)
        enriched = [deep_diagnostic.enrich_row(row) for row in rows]
        learned_or_hold = human_packet.known_learned_or_hold_segment_ids(conn)
        high_severity = human_packet.high_severity_open_issue_segment_ids(conn, [int(row["segment_id"]) for row in enriched])
        special = special_getter_omission(conn)
    target = [row for row in enriched if row.get("risk_bucket") == TARGET_RISK]
    records: list[dict[str, Any]] = []
    for row in target:
        operational_class, reason, reason_tags = classify(row, learned_or_hold, high_severity)
        records.append(
            {
                "segment_id": int(row["segment_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "source_line_number": row.get("source_line_number"),
                "surface_bucket": row.get("surface_bucket"),
                "risk_bucket": row.get("risk_bucket"),
                "token_count": row.get("token_count"),
                "output_token": output_token(row),
                "operational_class": operational_class,
                "classification_reason": reason,
                "classification_tags": reason_tags,
                "english_text": short(row.get("english_text")),
                "spanish_text": short(row.get("spanish_text")),
                "current_output_text": short(row.get("current_output_text")),
                "recommended_handling": {
                    "token_simples_preservavel": "candidate for narrow human packet or parser-backed dry-run after sampling",
                    "getter_perspective_omitted": "architecture/perspective policy review before candidates",
                    "artigo_genero_token_leve": "human review unless article/gender policy exists",
                    "estilo_fluencia_token_leve": "small human packet before automation",
                    "precisa_humano": "human/domain review",
                    "precisa_arquitetura": "architecture/policy design",
                    "hold_contextual": "hold until blocking context changes",
                }[operational_class],
            }
        )
    records.sort(key=lambda row: (str(row["surface_bucket"]), str(row["operational_class"]), int(row["segment_id"])))
    sample = representative_samples(records)
    summary = build_summary(records, special, ledger_run_id, preflight_path, len(excluded_segment_ids), segment_state_run_id)
    txt_path, jsonl_path, summary_path = write_outputs(summary, records, sample)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"segment_state_run_id={segment_state_run_id}")
    print(f"ledger_run_id={ledger_run_id}")
    print(f"record_count={summary['record_count']}")
    print("operational_class_counts=" + json.dumps(summary["operational_class_counts"], ensure_ascii=False, sort_keys=True))
    print("surface_bucket_counts=" + json.dumps(summary["surface_bucket_counts"], ensure_ascii=False, sort_keys=True))
    print("recommended_next_prompt=" + summary["recommended_next_prompt"])
    print("production_full_recommended_now=false")


if __name__ == "__main__":
    main()
