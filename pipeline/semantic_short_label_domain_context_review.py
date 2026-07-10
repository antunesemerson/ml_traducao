from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!")
DYNAMIC_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|Select_CString|Custom\(|ScriptValue|Concept|"
    r"\bGet[A-Za-z0-9_]*\b|\b(?:ROOT|CHARACTER|TARGET|SCOPE|THIS)\.",
    re.IGNORECASE,
)
GENDER_RE = re.compile(
    r"ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|Get(?:SheHe|HerHis|WomanMan|WomenMen)|custom_loc",
    re.IGNORECASE,
)
VISIBLE_RESIDUAL_RE = re.compile(
    r"\b(?:hay|mientras|arriba|abajo|este|personaje|sangre|revueltas|libro|clave|"
    r"resolver|sera|gran|anadido|coleccion|ninguno|lo|bastante|miremos|mas|esos|"
    r"siguen|amargas|planificada|consigue|apodo|costes|tiene|por que|asi|esta|"
    r"podria|conceder|exencion|tendre|encontrar|alhaja|consigues|talla|hoja|"
    r"daga|util|rechazo|liebre|caballo|por fin|causando|impresion|vuelves|"
    r"vacias|escapas|collar|mantenerte|vela|supuesto|arriesgado|eleccion|"
    r"espias|exito|corazon|golpea|llamo|quizas|gustaria|cerca|salimos|"
    r"persigue|clavo|aterrizaje|se que|antes|oleada|oraciones|ayuno|"
    r"efectivas|fosas|mueven|letargo|acuerdo)\b",
    re.IGNORECASE,
)
TOKEN_BOUNDARY_RE = re.compile(r"\w\?\w|[\[\]]{2,}|\$\s*\$")

TITLE_OR_LAW_RE = re.compile(
    r"law|order|modifier|control|capital|councillor|steward|stewardship|diarchy|"
    r"domain|duty|wealth|martial|authority|strategy|war_event|war_events|warfare|"
    r"realm|government|succession|tax|impuestos|investment|inversion|authority",
    re.IGNORECASE,
)
CULTURE_RE = re.compile(r"culture|tradition|innovation|heritage|ethos", re.IGNORECASE)
RELIGION_RE = re.compile(
    r"religion|faith|doctrine|tenet|clergy|prayer|oraciones|ayuno|fast|pilgrimage",
    re.IGNORECASE,
)
NAME_RE = re.compile(
    r"nickname|dynasty|house|trait|character|personaje|lu_bu|lu bu|caramelito|apodo",
    re.IGNORECASE,
)
ARTIFACT_ACTIVITY_RE = re.compile(
    r"artifact|activity|activities|tour|travel|tournament|legend|trinket|artifact_events|"
    r"warhorse|collection|coleccion|collar|book|libro|puzzle|enigma|alhaja|talla|"
    r"herramienta|arma|daga|caballo",
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r"event|\.desc|desc\.|option|scheme|ongoing|outcome|flavour|intent|"
    r"interaction|memory|toast",
    re.IGNORECASE,
)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            final_state,
            state_group,
            needs_output_apply,
            confirmed_matches_output,
            needs_reopen,
            is_closed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def fetch_family_shapes(conn: sqlite3.Connection, ledger_run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            COUNT(*) AS open_issue_count,
            SUM(CASE WHEN issue_family = 'semantic_review_router' THEN 1 ELSE 0 END) AS semantic_count,
            SUM(CASE WHEN issue_family = 'short_label_style_microagent' THEN 1 ELSE 0 END) AS short_label_count,
            SUM(CASE WHEN issue_family NOT IN ('semantic_review_router', 'short_label_style_microagent') THEN 1 ELSE 0 END) AS other_family_count
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND status = 'open'
          AND segment_id IN ({placeholders})
        GROUP BY segment_id
        """,
        (ledger_run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def collect_domain_rows(combo_path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in read_jsonl(db.project_path(combo_path)):
        if row.get("decision") != "needs_domain_context":
            continue
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            continue
        seen.add(segment_id)
        rows.append(row)
    return rows


def has_exact_open_family_shape(family_shape: dict[str, Any] | None) -> bool:
    return bool(
        family_shape
        and int(family_shape.get("open_issue_count") or 0) == 2
        and int(family_shape.get("semantic_count") or 0) == 1
        and int(family_shape.get("short_label_count") or 0) == 1
        and int(family_shape.get("other_family_count") or 0) == 0
    )


def has_ready_state(state: dict[str, Any] | None) -> bool:
    return bool(
        state
        and state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
        and int(state.get("is_closed") or 0) == 0
    )


def domain_subpolicy(row: dict[str, Any]) -> tuple[str, str]:
    text = as_text(row.get("current_text"))
    haystack = " ".join([as_text(row.get("relative_path")), as_text(row.get("key")), text])
    path_key = " ".join([as_text(row.get("relative_path")), as_text(row.get("key"))])

    if DYNAMIC_RE.search(text) or DYNAMIC_RE.search(path_key):
        return "needs_dynamic_expression_agent", "dynamic_expression"
    if RELIGION_RE.search(haystack):
        return "needs_religion_policy", "faith_religion_doctrine_or_clergy"
    if CULTURE_RE.search(haystack):
        return "needs_culture_policy", "culture_tradition_innovation_heritage_or_ethos"
    if NAME_RE.search(haystack):
        return "needs_name_or_nickname_policy", "name_nickname_dynasty_house_or_character"
    if ARTIFACT_ACTIVITY_RE.search(haystack):
        return "needs_artifact_or_activity_policy", "artifact_activity_travel_tournament_or_legend"
    if TITLE_OR_LAW_RE.search(haystack):
        return "needs_title_or_law_policy", "title_law_government_or_realm"
    if EVENT_RE.search(haystack):
        return "needs_event_context_composer", "event_context"
    if VISIBLE_RESIDUAL_RE.search(text):
        return "needs_residual_repair", "visible_spanish_or_english_residual"
    return "needs_mixed_domain_semantic_review", "mixed_domain_semantic_review"


def ready_decision(row: dict[str, Any], state: dict[str, Any] | None, family_shape: dict[str, Any] | None) -> str | None:
    text = as_text(row.get("current_text"))
    haystack = " ".join([as_text(row.get("relative_path")), as_text(row.get("key")), text])
    if not has_ready_state(state) or not has_exact_open_family_shape(family_shape):
        return None
    if GENDER_RE.search(haystack) or DYNAMIC_RE.search(text) or TOKEN_BOUNDARY_RE.search(text):
        return None
    if VISIBLE_RESIDUAL_RE.search(text):
        return None
    if TOKEN_RE.findall(text) and text.count("[") != text.count("]"):
        return None
    if int(state.get("needs_reopen") or 0) == 1:
        return "semantic_short_label_domain_ready_false_reopen"
    return "semantic_short_label_domain_ready_named_concept_lifecycle"


def decide(row: dict[str, Any], state: dict[str, Any] | None, family_shape: dict[str, Any] | None) -> dict[str, Any]:
    ready = ready_decision(row, state, family_shape)
    if ready:
        return {
            "domain_decision": ready,
            "domain_subpolicy": "domain_ready_false_reopen" if ready.endswith("false_reopen") else "named_domain_concept",
            "requires_lifecycle_later": True,
            "requires_apply_later": False,
            "notes": "domain context appears aligned and suitable for future narrow lifecycle",
        }

    decision, subpolicy = domain_subpolicy(row)
    if not has_ready_state(state):
        notes = "blocked by segment_state guard; kept out of ready lifecycle"
    elif not has_exact_open_family_shape(family_shape):
        notes = "blocked by open issue family shape guard; kept out of ready lifecycle"
    else:
        notes = f"routed to {decision}; no apply or lifecycle emitted by this review"
    return {
        "domain_decision": decision,
        "domain_subpolicy": subpolicy,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_semantic_short_label_domain_context_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["domain_decision"] for row in rows)
    subpolicy_counts = Counter(row["domain_subpolicy"] for row in rows)
    ready_count = sum(1 for row in rows if row["domain_decision"].startswith("semantic_short_label_domain_ready_"))

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    if ready_count >= 10:
        recommendation = "prepare_narrow_readonly_lifecycle"
    elif subpolicy_counts and subpolicy_counts.most_common(1)[0][1] >= 15:
        recommendation = f"prepare_specific_policy_microagent:{subpolicy_counts.most_common(1)[0][0]}"
    else:
        recommendation = "migrate_to_combo_dynamic_expression_agent"

    lines = [
        "Semantic short-label domain context review",
        "",
        f"total_reviewed: {len(rows)}",
        "",
        "Decision counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(decision_counts.items()))
    lines.extend(["", "Subpolicy counts:"])
    lines.extend(f"- {key}: {value}" for key, value in sorted(subpolicy_counts.items()))
    lines.extend(
        [
            "",
            f"ready_for_future_lifecycle: {ready_count}",
            "apply_candidates_future: 0",
            f"Recommendation: {recommendation}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--combo-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    args = parser.parse_args()

    source_rows = collect_domain_rows(args.combo_jsonl)
    segment_ids = [int(row["segment_id"]) for row in source_rows]
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, segment_ids)
    family_shapes = fetch_family_shapes(conn, args.ledger_run_id, segment_ids)

    reviewed: list[dict[str, Any]] = []
    for row in source_rows:
        segment_id = int(row["segment_id"])
        decision = decide(row, states.get(segment_id), family_shapes.get(segment_id))
        reviewed.append(
            {
                "segment_id": segment_id,
                "key": row["key"],
                "relative_path": row["relative_path"],
                "current_text": row["current_text"],
                "source_decision": "needs_domain_context",
                **decision,
            }
        )

    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)
    ready_count = sum(1 for row in reviewed if row["domain_decision"].startswith("semantic_short_label_domain_ready_"))
    print(f"total_reviewed={len(reviewed)}")
    print(f"ready_for_future_lifecycle={ready_count}")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print(f"decision_counts={json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True)}")
    print(f"subpolicy_counts={json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
