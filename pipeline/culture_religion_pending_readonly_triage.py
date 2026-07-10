from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "culture_religion_pending_readonly_triage_v1"
SEGMENT_STATE_RUN_ID = 410
TARGET_PATHS = [
    "culture/traditions/cultural_traditions_l_spanish.yml",
    "religion/religion_l_spanish.yml",
]
SAMPLE_PER_LANE = 8
KNOWN_HOLD_SEGMENTS = {
    21002: "needs_more_context_knight_culture_player_plural",
    21003: "needs_more_context_knight_culture_player_plural",
    21004: "needs_more_context_knight_culture_player_plural",
    239966: "needs_more_context_knight_culture_player_plural",
    239970: "needs_more_context_knight_culture_player_plural",
    240178: "needs_more_context_knight_culture_player_plural",
}

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|SelectLocalization\([^)]*\)|AddLocalizationIf\([^)]*\)|"
    r"\b(?:ROOT|FROM|SCOPE|TARGET|CHARACTER|THIS)\.|Get[A-Za-z0-9_]+|"
    r"ScriptValue|Concept",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[\wÀ-ÿ']+", re.UNICODE)
SPANISH_RESIDUAL_RE = re.compile(
    r"\b(?:el|la|los|las|un|una|del|dela|es|son|puede|pueden|solo|hombres|mujeres|"
    r"verdadero|verdadera|actual|siguiente|coste|contra|propios)\b",
    re.IGNORECASE,
)
ENGLISH_RESIDUAL_RE = re.compile(
    r"\b(?:the|of|and|or|can|cannot|will|must|only|both|more|less|current|next)\b",
    re.IGNORECASE,
)
DESC_KEY_RE = re.compile(r"(?:^|_)desc(?:_|$)|description", re.IGNORECASE)
PARAMETER_KEY_RE = re.compile(r"parameter|modifier|doctrine|culture_parameter", re.IGNORECASE)
REQUIREMENT_KEY_RE = re.compile(r"requirement|unlock|valid|invalid|cannot|can_be|not_under|must_be", re.IGNORECASE)
GENDER_DYNAMIC_RE = re.compile(r"\$knight_culture_player_plural\$|ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|Select_CString", re.IGNORECASE)
FAITH_CULTURE_RE = re.compile(r"faith|religion|doctrine|clergy|temple|fervor|culture|tradition|ethos|martial", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def short(value: Any, limit: int = 260) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def token_list(value: str) -> list[str]:
    return TOKEN_RE.findall(value or "")


def word_count(value: str) -> int:
    return len(WORD_RE.findall(TOKEN_RE.sub(" ", value or "")))


def fetch_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in TARGET_PATHS)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT
                state.segment_id,
                state.relative_path,
                state.source_key,
                state.source_line_number,
                state.final_state,
                state.state_group,
                state.review_state,
                state.apply_state,
                state.needs_output_apply,
                state.confirmed_matches_output,
                state.priority_score,
                source.english_text,
                source.spanish_text,
                source.old_text,
                output.portuguese_text AS current_output_text
            FROM segment_state_items state
            LEFT JOIN source_segments source ON source.id = state.segment_id
            LEFT JOIN output_segments output ON output.segment_id = state.segment_id
            WHERE state.run_id = ?
              AND state.state_group = 'pending'
              AND state.final_state = 'reopen_auto_confirmed_autofix'
              AND state.relative_path IN ({placeholders})
            ORDER BY state.relative_path, state.priority_score DESC, state.segment_id
            """,
            (SEGMENT_STATE_RUN_ID, *TARGET_PATHS),
        )
    ]


def lane_for(row: dict[str, Any]) -> tuple[str, str, str]:
    segment_id = int(row["segment_id"])
    key = str(row.get("source_key") or "")
    text = str(row.get("current_output_text") or "")
    english = str(row.get("english_text") or "")
    spanish = str(row.get("spanish_text") or "")
    haystack = " ".join([key, text, english, spanish])
    tokens = token_list(text)
    wc = word_count(text)

    if segment_id in KNOWN_HOLD_SEGMENTS:
        return "known_hold_context_risk", KNOWN_HOLD_SEGMENTS[segment_id], "architecture_or_explicit_context_review"
    if GENDER_DYNAMIC_RE.search(haystack):
        return "dynamic_gender_or_custom_loc", "dynamic gender/custom localization surface", "architecture_or_context_policy"
    if SPANISH_RESIDUAL_RE.search(text) or ENGLISH_RESIDUAL_RE.search(text):
        return "residual_language_surface", "possible visible source-language residue", "human_residual_review"
    if DESC_KEY_RE.search(key) or wc >= 24 or "\n" in text:
        return "long_description_context", "long/domain description needs semantic context", "human_or_domain_review"
    if REQUIREMENT_KEY_RE.search(key) and tokens:
        return "requirement_or_rule_tooltip_tokenized", "requirement/rule tooltip with protected tokens", "guarded_policy_review"
    if PARAMETER_KEY_RE.search(key):
        return "parameter_or_modifier_label", "parameter/modifier label likely patterned", "readonly_sample_for_policy"
    if FAITH_CULTURE_RE.search(haystack) and tokens:
        return "domain_tokenized_short_label", "culture/religion short label with domain token", "guarded_human_sample"
    if tokens:
        return "light_token_short_label", "short tokenized label", "guarded_human_sample"
    if wc <= 8 and len(text) <= 90:
        return "plain_short_label", "plain short label without protected token", "human_sample_or_policy"
    return "plain_or_medium_context", "plain/medium context requiring review", "human_sample"


def enrich(row: dict[str, Any]) -> dict[str, Any]:
    text = str(row.get("current_output_text") or "")
    lane, rationale, next_action = lane_for(row)
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "lane": lane,
        "rationale": rationale,
        "next_action": next_action,
        "token_count": len(token_list(text)),
        "word_count": word_count(text),
        "text_length": len(text),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "priority_score": float(row.get("priority_score") or 0.0),
        "current_output_text": short(text),
        "english_text": short(row.get("english_text")),
        "spanish_text": short(row.get("spanish_text")),
    }


def recommend(enriched: list[dict[str, Any]]) -> dict[str, Any]:
    lane_counts = Counter(row["lane"] for row in enriched)
    # Prefer routes that can yield clear decision, but avoid known context-risk holds.
    priority = [
        "parameter_or_modifier_label",
        "plain_short_label",
        "light_token_short_label",
        "domain_tokenized_short_label",
        "requirement_or_rule_tooltip_tokenized",
        "long_description_context",
        "dynamic_gender_or_custom_loc",
        "known_hold_context_risk",
    ]
    selected = next((lane for lane in priority if lane_counts.get(lane)), None)
    if selected in {"dynamic_gender_or_custom_loc", "known_hold_context_risk"}:
        next_step = "parar e preparar pauta para arquitetura/contexto antes de qualquer candidato"
    elif selected in {"parameter_or_modifier_label", "plain_short_label"}:
        next_step = "gerar uma amostra read-only pequena para revisão humana/política; sem candidato nem apply no mesmo ciclo"
    else:
        next_step = "gerar amostra read-only por lane para decidir entre revisão humana e arquitetura"
    return {
        "recommended_lane": selected,
        "recommended_lane_count": int(lane_counts.get(selected or "", 0)),
        "next_step": next_step,
        "candidate_generation_recommended_now": False,
        "apply_recommended_now": False,
        "reindex_recommended_now": False,
        "production_full_recommended_now": False,
    }


def build_summary() -> dict[str, Any]:
    with connect_readonly() as conn:
        rows = fetch_rows(conn)
    enriched = [enrich(row) for row in rows]
    lane_counts = Counter(row["lane"] for row in enriched)
    path_counts = Counter(row["relative_path"] for row in enriched)
    next_action_counts = Counter(row["next_action"] for row in enriched)
    sample_by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in enriched:
        if len(sample_by_lane[row["lane"]]) < SAMPLE_PER_LANE:
            sample_by_lane[row["lane"]].append(row)
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "target_paths": TARGET_PATHS,
        "rows_reviewed": len(enriched),
        "path_counts": [{"key": key, "count": value} for key, value in path_counts.most_common()],
        "lane_counts": [{"key": key, "count": value} for key, value in lane_counts.most_common()],
        "next_action_counts": [{"key": key, "count": value} for key, value in next_action_counts.most_common()],
        "known_hold_count": sum(1 for row in enriched if row["segment_id"] in KNOWN_HOLD_SEGMENTS),
        "sample_by_lane": dict(sample_by_lane),
        "read_only": True,
        "candidate_generation_executed": False,
        "apply_executed": False,
        "reindex_executed": False,
        "production_full_executed": False,
    }
    summary["recommendation"] = recommend(enriched)
    return summary, enriched


def write_outputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_culture_religion_pending_readonly_triage"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "culture/religion pending read-only triage",
        f"source={RULE_VERSION}",
        f"segment_state_run_id={SEGMENT_STATE_RUN_ID}",
        f"rows_reviewed={summary['rows_reviewed']}",
        f"known_hold_count={summary['known_hold_count']}",
        "",
        "path_counts:",
        *[f"- {item['key']}: {item['count']}" for item in summary["path_counts"]],
        "",
        "lane_counts:",
        *[f"- {item['key']}: {item['count']}" for item in summary["lane_counts"]],
        "",
        "next_action_counts:",
        *[f"- {item['key']}: {item['count']}" for item in summary["next_action_counts"]],
        "",
        "recommendation:",
        f"- recommended_lane={summary['recommendation']['recommended_lane']}",
        f"- recommended_lane_count={summary['recommendation']['recommended_lane_count']}",
        f"- next_step={summary['recommendation']['next_step']}",
        f"- candidate_generation_recommended_now={str(summary['recommendation']['candidate_generation_recommended_now']).lower()}",
        f"- apply_recommended_now={str(summary['recommendation']['apply_recommended_now']).lower()}",
        f"- reindex_recommended_now={str(summary['recommendation']['reindex_recommended_now']).lower()}",
        f"- production_full_recommended_now={str(summary['recommendation']['production_full_recommended_now']).lower()}",
        "",
        "sample:",
    ]
    for lane, sample_rows in summary["sample_by_lane"].items():
        lines.append(f"- lane={lane}")
        for row in sample_rows[:3]:
            lines.append(f"  - {row['segment_id']} {row['source_key']}: {row['current_output_text']}")
    lines.extend(
        [
            "",
            "execution_flags:",
            "- read_only=true",
            "- candidate_generation_executed=false",
            "- apply_executed=false",
            "- reindex_executed=false",
            "- production_full_executed=false",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    summary, rows = build_summary()
    txt_path, jsonl_path, summary_path = write_outputs(summary, rows)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"rows_reviewed={summary['rows_reviewed']}")
    print(f"known_hold_count={summary['known_hold_count']}")
    print(f"recommended_lane={summary['recommendation']['recommended_lane']}")
    print(f"recommended_lane_count={summary['recommendation']['recommended_lane_count']}")
    print(f"next_step={summary['recommendation']['next_step']}")
    print(f"production_full_recommended_now={summary['recommendation']['production_full_recommended_now']}")


if __name__ == "__main__":
    main()
